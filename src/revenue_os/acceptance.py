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

import re
from pathlib import Path

from . import opportunity_state as ostate
from .events import load_events
from .execution import TASK_STATUSES, load_tasks
from .opportunity_store import load_opportunities
from .task_class import classify_task

# The DEPLOY approval bucket is NOT hard-coded here - it is derived from the
# central Phase-6 classifier (publishing the commercial landing page on the
# owner's own hosting -> EXTERNAL_AUTHORIZED, gated on a "money" approval).
_DEPLOY_APPROVAL = classify_task("DEPLOY").approval_type

# (task_type, dependency task_types, approval_type or "")
CHAIN: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("PLAN",             (),                              ""),
    ("BUILD_PRODUCT",    ("PLAN",),                       ""),
    ("BUILD_PAGE",       ("PLAN",),                       ""),
    ("VALIDATE_PRODUCT", ("BUILD_PRODUCT",),              ""),
    ("VALIDATE_PAGE",    ("BUILD_PAGE",),                 ""),
    ("DEPLOY",           ("BUILD_PAGE", "VALIDATE_PAGE"), _DEPLOY_APPROVAL),
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

#: a plausible email shape - the same check used elsewhere in the repo
#: (deliverable._clean_email / paypal_payments._EMAIL_RE) for a value that
#: will be persisted and used as a delivery recipient.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _valid_email(value: str) -> str:
    """`value` if it is a plausible email address, else ""."""
    v = str(value or "").strip()
    return v if _EMAIL_RE.match(v) else ""


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
    q.unblock(task_id, by=actor)
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


def deliver_now(data_dir, opportunity_id: str, *, payment_ref: str = "",
                customer_ref: str = "", adapter=None, actor: str = "human") -> dict:
    """Human action: complete a real product delivery for a confirmed sale,
    OUTSIDE the autonomous worker loop (Phase 11-real P1-7).

    `customer_ref` (Phase 11-real P1-12): an OPTIONAL explicit buyer email
    the human supplies (`deliver-product --email`) for the real-world case
    where PayPal's Transaction Search returned no `payer_info.email_address`
    (the buyer's privacy settings), so the auto-spawned DELIVER task has an
    empty `customer_ref`. It is used ONLY when the DELIVER task itself
    carries no customer reference - a reference already captured from the
    payment always wins and is never overridden. The value is validated as
    a plausible email and the delivery still fails closed if neither source
    yields one.

    The worker's own DELIVER task is `SAFE_AUTONOMOUS` (task_class.py) on
    the assumption that the delivery-adapter layer is fail-closed - true
    by default (`default_delivery_adapter()` is `NullDeliveryAdapter`) and
    still true even with a real `SmtpDeliveryAdapter` injected, because
    that adapter's own `guard_no_money_in_autonomy` unconditionally
    refuses inside `autonomous_context()` (Phase 11-real P1-3). A real
    send can therefore only ever happen from a code path that never
    enters `autonomous_context()` at all - exactly like
    `deploy.deploy_checkout()` / `delivery.send_delivery()` for the
    candidate path. This function is that path for Opportunities: a
    plain, synchronous, human-triggered call, never used by the worker.

    Reuses the SAME `record_delivery()` / DELIVERING -> ACTIVE transition
    calls the worker's own `_on_success()` makes for a successful DELIVER
    - no parallel data model. The original DELIVER task's own status is
    deliberately left as-is (terminal statuses have an empty legal-
    transition set by design - execution.py's _LEGAL table - so it can
    never be rewritten to SUCCEEDED): it stays an accurate record that
    the autonomous attempt failed; this human delivery is tracked
    separately via `record_delivery`, which is exactly what
    `DeliverTaskAdapter`'s own idempotency check already reads.

    Fails closed (raises AcceptanceError, a ValueError) on: unknown
    opportunity, no matching (or an ambiguous) DELIVER task, missing/
    empty customer reference, a missing product file, or the adapter
    itself failing or being blocked - never fabricates a delivery.
    Idempotent: a payment_ref already recorded as a successful delivery
    is a no-op.
    """
    from .delivery_adapters import DeliveryArtifact, DeliveryRecipient, SmtpDeliveryAdapter

    data_dir = Path(data_dir)
    store = load_opportunities(data_dir)
    rec = store.get(opportunity_id)
    if rec is None:
        raise AcceptanceError(f"unknown opportunity {opportunity_id!r}")

    q = load_tasks(data_dir)
    candidates = [t for t in q.by_opportunity(opportunity_id) if t.task_type == "DELIVER"]
    if payment_ref:
        candidates = [t for t in candidates
                     if str((t.input or {}).get("payment_ref", "")) == payment_ref]
    if not candidates:
        raise AcceptanceError(
            f"no DELIVER task found for {opportunity_id!r}"
            + (f" with payment_ref {payment_ref!r}" if payment_ref else ""))
    if len(candidates) > 1:
        raise AcceptanceError(
            f"{len(candidates)} DELIVER tasks exist for {opportunity_id!r} - "
            "pass payment_ref to disambiguate")
    task = candidates[0]
    pref = str((task.input or {}).get("payment_ref", ""))
    if not pref:
        raise AcceptanceError(f"DELIVER task {task.task_id} has no payment_ref")

    prior = ((rec.get("execution") or {}).get("deliveries") or {}).get(pref)
    if prior and prior.get("success"):
        return {"opportunity_id": opportunity_id, "payment_ref": pref,
                "outcome": "already_delivered", "delivery": prior,
                "state": rec.get("state")}

    # An override that is present but not a plausible email is a hard error
    # (a human typo must not silently fall through to "no reference").
    override_ref = ""
    if customer_ref:
        override_ref = _valid_email(customer_ref)
        if not override_ref:
            raise AcceptanceError(
                f"customer_ref {customer_ref!r} is not a valid email address")

    task_customer_ref = str((task.input or {}).get("customer_ref", ""))
    recipient_ref = task_customer_ref or override_ref
    if not recipient_ref:
        raise AcceptanceError(
            f"DELIVER task {task.task_id} has no customer reference "
            "(the PayPal payment carried no buyer email) - pass an explicit "
            "--email / customer_ref to deliver")

    product_path = data_dir / "deliverables" / opportunity_id / "product.md"
    if not product_path.is_file():
        raise AcceptanceError(f"no product file at {product_path} - nothing to deliver")

    live_url = (rec.get("execution") or {}).get("live_url", "")
    artifact = DeliveryArtifact(
        opportunity_id=opportunity_id, product_name=rec.get("title", "your purchase"),
        live_url=live_url,
        body=(f"Thank you for your purchase of \"{rec.get('title', '')}\".\n\n"
              "Your product is attached (product.md).\n"),
        files={"product.md": product_path.read_bytes()})
    recipient = DeliveryRecipient(reference=recipient_ref, opportunity_id=opportunity_id)

    delivery_adapter = adapter if adapter is not None else SmtpDeliveryAdapter()
    result = delivery_adapter.deliver(artifact, recipient)
    if not result.success:
        raise AcceptanceError(
            f"delivery {'blocked' if result.blocked else 'failed'}: {result.error}")

    out = result.to_dict()
    store.record_delivery(opportunity_id, pref, out)
    store.save()

    # NOTE: the ExecutionTask's own status is deliberately left untouched.
    # TaskQueue's terminal statuses (SUCCEEDED / FAILED_FINAL / CANCELLED)
    # have an empty legal-transition set by design (execution.py's _LEGAL
    # table) - once terminal, never changes again, so the record stays a
    # historically accurate audit trail: the AUTONOMOUS attempt genuinely
    # failed (no provider configured). This human delivery is a separate,
    # out-of-band fact, correctly captured by `record_delivery` (the same
    # field DeliverTaskAdapter's own idempotency check reads) and the
    # opportunity state transition below - not by rewriting task history.

    ev = load_events(data_dir)
    ev.emit("DELIVERY_COMPLETE", task_id=task.task_id, opportunity_id=opportunity_id,
            task_type="DELIVER", actor=actor, payment_ref=pref,
            delivery_id=out.get("delivery_id", ""), reference=out.get("reference", ""),
            recipient=out.get("recipient", ""), provider=out.get("provider", ""),
            idempotent=False)

    for tgt, why in (("DELIVERING", "delivery confirmed"), ("ACTIVE", "delivery complete")):
        cur = (store.get(opportunity_id) or {}).get("state") or ostate.INITIAL
        if ostate.can_transition(cur, tgt):
            tr = store.transition(opportunity_id, tgt, reason=f"DELIVER: {why}",
                                  source="human", actor=actor, task_id=task.task_id)
            ev.emit("OPPORTUNITY_TRANSITIONED", task_id=task.task_id,
                    opportunity_id=opportunity_id, task_type="DELIVER", actor=actor,
                    **{"from": tr["previous_state"], "to": tr["next_state"],
                       "reason": tr["reason"]})
    store.save()
    ev.save()

    return {"opportunity_id": opportunity_id, "payment_ref": pref,
            "outcome": "delivered", "delivery": out,
            "customer_ref": recipient_ref,
            "customer_ref_source": "payment" if task_customer_ref else "override",
            "state": store.get(opportunity_id).get("state")}


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


#: the exact, unmodified error prefix CheckRevenueAdapter emits when its
#: configured PaymentAdapter reports blocked=True (NullPaymentAdapter's
#: permanent state - task_adapters.py). Matching on this is the only
#: signal used for the CHECK_PAYMENTS recommendation below: an already-
#: recorded fact (a real task genuinely failed this way), never a guess
#: about elapsed time.
_CHECK_REVENUE_BLOCKED_MARKER = "payment check BLOCKED"


def pending_actions(data_dir) -> list[dict]:
    """Phase 11-real P1-9: read-only visibility over concrete, already-
    recorded Execution/Opportunity state that names an existing CLI
    action a human could take next. Never executes, schedules, or
    guesses anything - every row is derived from a fact already
    persisted by the real architecture:

      RELEASE_TASK    - `execution_view()`'s own `blocked_task_id`
                        (a task is literally sitting BLOCKED_APPROVAL)
      CHECK_PAYMENTS  - this opportunity's MOST RECENT CHECK_REVENUE
                        task is FAILED_FINAL with the exact error
                        CheckRevenueAdapter emits when its (Null, by
                        default) PaymentAdapter reports blocked - never
                        "it has been a while since the last check"
      DELIVER_PRODUCT - a DELIVER task exists whose payment_ref has no
                        recorded successful delivery in
                        `execution.deliveries` (the SAME field
                        `deliver_now()`'s own idempotency check reads -
                        no new delivery logic)

    Reuses `execution_view()` as the primary per-opportunity source;
    reads the TaskQueue/OpportunityStore directly only for the per-task
    `input` / `execution.deliveries` fields `execution_view()` does not
    expose. Performs no mutation and calls no adapter.
    """
    data_dir = Path(data_dir)
    rows = execution_view(data_dir)
    q = load_tasks(data_dir)
    store = load_opportunities(data_dir)

    out: list[dict] = []
    for row in rows:
        oid = row["opportunity_id"]
        title = row.get("title", "")

        if row.get("blocked_task_id"):
            out.append({
                "action": "RELEASE_TASK",
                "opportunity_id": oid,
                "title": title,
                "detail": row.get("blocker", "a task needs approval"),
                "command": ("no CLI release command exists yet - release task "
                           f"{row['blocked_task_id']!r} via the JARVIS "
                           "dashboard's approval button"),
            })

        opp_tasks = q.by_opportunity(oid)

        check_revenue_tasks = sorted(
            (t for t in opp_tasks if t.task_type == "CHECK_REVENUE"),
            key=lambda t: t.created_at)
        if check_revenue_tasks:
            latest = check_revenue_tasks[-1]
            if (latest.status == "FAILED_FINAL"
                    and _CHECK_REVENUE_BLOCKED_MARKER in (latest.error or "")):
                out.append({
                    "action": "CHECK_PAYMENTS",
                    "opportunity_id": oid,
                    "title": title,
                    "detail": f"CHECK_REVENUE task {latest.task_id} is "
                              f"FAILED_FINAL: {latest.error}",
                    "command": "revenue_os check-payments",
                })

        rec = store.get(oid) or {}

        verify_tasks = sorted(
            (t for t in opp_tasks if t.task_type == "VERIFY_RESULT"),
            key=lambda t: t.created_at)
        if verify_tasks and verify_tasks[-1].status == "SUCCEEDED":
            already_recorded = any(
                e.get("kind") == "task_outcome" for e in rec.get("experiments") or [])
            if not already_recorded:
                deliverable = (verify_tasks[-1].output or {}).get(
                    "deliverable_path", f"deliverables/{oid}/task_solution.md")
                d = rec.get("discovery") or {}
                source_url = d.get("source_url", "")
                source = str(d.get("source", ""))
                platform = source.split("human_fed:", 1)[1] if source.startswith(
                    "human_fed:") else ""
                pay = d.get("payment_evidence") or {}
                policy_status = d.get("policy_status", "")

                detail = (f"verified deliverable ready at {deliverable}"
                         + (f" - a human submits it at {source_url}"
                            if source_url else " - a human submits it on "
                            "the source platform")
                         + "; the fleet never submits it (platform "
                         "rules/login/TOS) - final submission requires "
                         "human action")
                if policy_status in ("HUMAN_REQUIRED", "BLOCKED_BY_POLICY"):
                    detail += (
                        f" | COMPLIANCE WARNING: this platform's own terms "
                        f"are {policy_status} for AI-assisted preparation - "
                        "review before submitting, see "
                        "ecosystem.human_fed.PLATFORM_POLICY for the citation")
                out.append({
                    "action": "SUBMIT_TASK",
                    "opportunity_id": oid,
                    "title": title,
                    "platform": platform,
                    "task_url": source_url,
                    "payment": {"amount": pay.get("amount", 0),
                               "currency": pay.get("currency", "")},
                    "deliverable_path": deliverable,
                    "detail": detail,
                    "command": f"revenue_os record-task-outcome {oid} --success "
                               "--amount <EUR> --ref <payment-reference> "
                               f"(or --failure --note <reason> if it was not paid)",
                })

        deliveries = (rec.get("execution") or {}).get("deliveries") or {}
        for t in opp_tasks:
            if t.task_type != "DELIVER":
                continue
            pref = str((t.input or {}).get("payment_ref", ""))
            if not pref:
                continue
            prior = deliveries.get(pref)
            if prior and prior.get("success"):
                continue        # already delivered - nothing pending
            out.append({
                "action": "DELIVER_PRODUCT",
                "opportunity_id": oid,
                "title": title,
                "detail": f"DELIVER task {t.task_id} ({t.status}) for payment "
                          f"{pref!r} has no recorded successful delivery",
                "command": f"revenue_os deliver-product {oid} --payment-ref {pref}",
            })

    return out
