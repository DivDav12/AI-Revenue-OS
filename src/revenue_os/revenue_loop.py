"""The revenue supervisor: OBSERVE -> DECIDE -> ACT (one step) -> PERSIST.

This is the loop the audit said was missing. It does NOT remove any
human gate and it does NOT invent work: each step it reads the real
candidate / pipeline / payment / intake / delivery state, picks the
single most useful action that is technically allowed WITHOUT a human,
runs exactly that, and saves. When the only things left need a person it
STOPS and returns a concrete action queue.

Actions it will take on its own (all already implemented, all safe -
no money, no messages, no PayPal writes):

  stage_delivery  render an approved plan to a PDF on disk (no send)
  run_pipeline    select -> ... -> QC -> deploy the checkout page
  sync_payments   read-only PayPal booking (only if credentials present)
  discover        one free autopilot cycle (discovery + outreach drafts)

Everything else - approve, launch, build-checkout, post outreach,
intake-review, draft-launch-plan (costs money), plan-approve,
plan-deliver --send - stays a human gate and is surfaced, never done.

State: data/revenue_loop.json (atomic, restart-safe).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .store import CandidateStore, now_iso

logger = logging.getLogger(__name__)

_QUALIFIED = ("validated", "launched", "earning")
_STATUS_RANK = {"earning": 3, "launched": 2, "validated": 1}
_ONCE_PER_RUN = {"sync_payments", "discover"}


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _save_state(data_dir: Path, state: dict) -> None:
    path = data_dir / "revenue_loop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state, indent=2))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _blank_state() -> dict:
    return {"status": "idle", "steps_taken": 0, "started_at": None,
            "last_action": None, "last_reason": None, "history": [],
            "human_queue": []}


def load_state(data_dir) -> dict:
    return _load_json(Path(data_dir) / "revenue_loop.json") or _blank_state()


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------

def observe(data_dir) -> dict:
    data_dir = Path(data_dir)
    store = CandidateStore.load(data_dir / "candidates.json")
    cands = []
    for c in store.all():
        page = data_dir / "deliverables" / c.name
        cands.append({
            "name": c.name, "status": c.status, "has_offer": bool(c.offer),
            "public_url": c.public_url or None,
            "checkout_built": (page / "checkout.html").is_file(),
        })

    pipe = _load_json(data_dir / "pipeline.json") or {}
    intake_raw = _load_json(data_dir / "intake.json") or []
    intakes = [{
        "order_id": e.get("order_id"),
        "status": e.get("status"),
        "plan_status": (e.get("plan") or {}).get("status"),
        "candidate": e.get("candidate"),
    } for e in intake_raw]

    deliveries = {d.get("order_id"): d.get("status")
                  for d in (_load_json(data_dir / "deliveries.json") or [])}

    outreach = _load_json(data_dir / "outreach.json") or []
    awaiting_post = [b.get("lead_id") for b in outreach
                     if b.get("status") in ("draft", "approved")]

    ledger = _load_json(data_dir / "revenue.json") or []
    revenue_total = round(sum(float(e.get("amount", 0)) for e in ledger), 2)

    return {
        "candidates": cands,
        "pipeline": {"candidate": pipe.get("candidate"),
                     "status": pipe.get("status"),
                     "human_gate": pipe.get("human_gate")},
        "intakes": intakes,
        "deliveries": deliveries,
        "outreach_awaiting_post": awaiting_post,
        "revenue_total": revenue_total,
        "paypal_creds": bool(os.environ.get("PAYPAL_CLIENT_ID")
                             and os.environ.get("PAYPAL_CLIENT_SECRET")),
    }


# ---------------------------------------------------------------------------
# decide (pure)
# ---------------------------------------------------------------------------

def _human_queue(state: dict) -> list[str]:
    q: list[str] = []
    for c in state["candidates"]:
        if c["status"] == "shortlisted":
            q.append(f"approve or reject candidate: `revenue_os approve {c['name']}`")
        elif c["status"] == "investigating":
            q.append(f"record a real validation outcome: "
                     f"`revenue_os outcome {c['name']} validated --metric \"...\"`")
        elif c["status"] == "validated" and c["has_offer"] and not c["checkout_built"]:
            q.append(f"launch + build the checkout page: `revenue_os launch "
                     f"{c['name']}` then `revenue_os build-checkout {c['name']} "
                     f"--price 29.90 ...` (needs PAYPAL_ENV=live)")
        elif c["status"] == "validated" and not c["has_offer"]:
            q.append(f"attach an offer: `revenue_os prepare-launch` "
                     f"(candidate {c['name']})")

    p = state["pipeline"]
    if p["status"] == "blocked":
        issues = ((p.get("human_gate") or {}).get("blocking_issues")) or []
        q.append(f"pipeline BLOCKED for {p['candidate']}: {'; '.join(issues) or 'see QC'}")
    elif p["status"] == "prepared":
        hg = p.get("human_gate") or {}
        if hg.get("payment_ready"):
            q.append(f"checkout is LIVE ({hg.get('public_url')}) - drive traffic: "
                     f"review + post the outreach briefs in your own voice")
        elif not hg.get("public_url"):
            q.append(f"deploy the checkout page for {p['candidate']} "
                     f"(set GITHUB_TOKEN + GITHUB_PAGES_REPO, or run "
                     f"`revenue_os deploy-checkout {p['candidate']}`)")

    for lead in state["outreach_awaiting_post"]:
        q.append(f"post outreach brief {lead} yourself (community rules first) - "
                 f"`revenue_os outreach-brief {lead}`")

    for it in state["intakes"]:
        oid = it["order_id"]
        deliv = state["deliveries"].get(oid)
        if it["status"] == "new":
            q.append(f"review buyer intake {oid}: `revenue_os intake-review {oid}`")
        elif it["status"] == "reviewed" and not it["plan_status"]:
            q.append(f"draft the plan for {oid} (costs ~$1.50): "
                     f"`revenue_os draft-launch-plan {oid}`")
        elif it["plan_status"] == "draft":
            q.append(f"approve the plan for {oid}: `revenue_os plan-approve {oid}`")
        elif it["plan_status"] == "approved" and deliv == "staged":
            q.append(f"send the delivered PDF for {oid}: "
                     f"`revenue_os plan-deliver {oid} --send`")
    return q


def decide(state: dict, *, done_once: set[str] | None = None,
           allow_discovery: bool = True,
           discovery_cooldown_hours: float = 6.0) -> Decision:
    done_once = done_once or set()

    # 1. finish in-flight sales first: stage an approved, undelivered plan
    for it in state["intakes"]:
        oid = it["order_id"]
        deliv = state["deliveries"].get(oid)
        if (it["status"] == "reviewed" and it["plan_status"] == "approved"
                and deliv not in ("staged", "sent")
                and f"staged:{oid}" not in done_once):
            return Decision("stage_delivery",
                            f"order {oid} has an approved plan and no PDF yet",
                            {"order_id": oid})

    # 2. read-only payment detection (once per run, needs credentials)
    if (state["paypal_creds"] and "sync_payments" not in done_once
            and (any(c["public_url"] for c in state["candidates"])
                 or state["intakes"])):
        return Decision("sync_payments", "check PayPal for new captured payments")

    # 3. build + deploy: run the pipeline for the best qualified candidate
    p = state["pipeline"]
    ready = [c for c in state["candidates"]
             if c["status"] in _QUALIFIED and c["has_offer"]]
    ready.sort(key=lambda c: _STATUS_RANK.get(c["status"], 0), reverse=True)
    for c in ready:
        if f"pipeline:{c['name']}" in done_once:
            continue
        same = p["candidate"] == c["name"]
        if same and p["status"] in ("prepared", "blocked"):
            continue
        return Decision("run_pipeline",
                        f"{c['name']} ({c['status']}) is not through the pipeline yet",
                        {"candidate": c["name"]})

    # 4. refresh the top of the funnel (free), once per run
    if allow_discovery and "discover" not in done_once:
        return Decision("discover", "refresh leads + outreach drafts (free)",
                        {"discovery_cooldown_hours": discovery_cooldown_hours})

    return Decision("stop", "only human-gated actions remain")


# ---------------------------------------------------------------------------
# act (one safe action)
# ---------------------------------------------------------------------------

def act(data_dir, decision: Decision) -> dict:
    data_dir = Path(data_dir)
    a = decision.action

    if a == "stage_delivery":
        from .delivery import DeliveryError, stage_delivery
        try:
            r = stage_delivery(data_dir, decision.detail["order_id"])
            return {"ok": True, "staged": r["order_id"], "pdf": r["pdf_path"]}
        except DeliveryError as exc:
            return {"ok": False, "error": str(exc)}

    if a == "sync_payments":
        from .autopilot import _paypal_check
        r = _paypal_check(data_dir)
        return {"ok": bool(r.get("ok")), **r}

    if a == "run_pipeline":
        from .pipeline import run_pipeline
        rep = run_pipeline(data_dir, decision.detail["candidate"])
        return {"ok": rep["status"] not in ("failed",),
                "pipeline_status": rep["status"],
                "public_url": (rep.get("human_gate") or {}).get("public_url")}

    if a == "discover":
        from .autopilot import run_cycle
        r = run_cycle(data_dir, politeness_delay=0,
                      discovery_cooldown_hours=decision.detail.get(
                          "discovery_cooldown_hours", 6.0))
        return {"ok": True, "discovery": r.get("discovery"),
                "outreach": r.get("outreach"), "sale": r.get("sale", False),
                "experiments": r.get("experiments")}

    return {"ok": True, "noop": True}


# ---------------------------------------------------------------------------
# step / run
# ---------------------------------------------------------------------------

def step(data_dir, *, done_once: set[str] | None = None,
         allow_discovery: bool = True,
         discovery_cooldown_hours: float = 6.0,
         followup_days: float = 14.0) -> dict:
    data_dir = Path(data_dir)
    state = observe(data_dir)
    decision = decide(state, done_once=done_once, allow_discovery=allow_discovery,
                      discovery_cooldown_hours=discovery_cooldown_hours)
    result: dict = {}
    if decision.action != "stop":
        logger.info("revenue-loop: %s - %s", decision.action, decision.reason)
        result = act(data_dir, decision)

    # deterministic experiment feedback - open / correlate / sweep. Read-only,
    # no LLM, no PayPal, no network; introduces no new autonomous "act".
    result["feedback"] = _experiment_feedback(data_dir, followup_days=followup_days)

    loop = load_state(data_dir)
    loop["status"] = "stopped" if decision.action == "stop" else "running"
    loop["started_at"] = loop.get("started_at") or now_iso()
    loop["steps_taken"] = int(loop.get("steps_taken", 0)) + (
        0 if decision.action == "stop" else 1)
    loop["last_action"] = decision.action
    loop["last_reason"] = decision.reason
    loop["human_queue"] = _human_queue(state)
    entry = {"ts": now_iso(), "action": decision.action,
             "reason": decision.reason, "detail": decision.detail, "result": result}
    loop["history"] = (loop.get("history", []) + [entry])[-50:]
    _save_state(data_dir, loop)

    return {"action": decision.action, "reason": decision.reason,
            "detail": decision.detail, "result": result,
            "human_queue": loop["human_queue"], "observed": state}


def run(data_dir, *, max_steps: int = 25, allow_discovery: bool = True,
        discovery_cooldown_hours: float = 6.0,
        followup_days: float = 14.0) -> list[dict]:
    """Step until only human-gated actions remain (or max_steps)."""
    data_dir = Path(data_dir)
    done_once: set[str] = set()
    steps: list[dict] = []
    for _ in range(max(1, max_steps)):
        out = step(data_dir, done_once=done_once, allow_discovery=allow_discovery,
                   discovery_cooldown_hours=discovery_cooldown_hours,
                   followup_days=followup_days)
        steps.append(out)
        if out["action"] == "stop":
            break
        if out["action"] in _ONCE_PER_RUN:
            done_once.add(out["action"])
        if out["action"] == "run_pipeline":
            done_once.add(f"pipeline:{out['detail'].get('candidate')}")
        if out["action"] == "stage_delivery":
            done_once.add(f"staged:{out['detail'].get('order_id')}")
        # a failed action must not spin
        if out["result"].get("ok") is False:
            done_once.add(out["action"])
            if out["action"] == "run_pipeline":
                done_once.add(f"pipeline:{out['detail'].get('candidate')}")
            if out["action"] == "stage_delivery":
                done_once.add(f"staged:{out['detail'].get('order_id')}")
    return steps


# ---------------------------------------------------------------------------
# experiment feedback (deterministic, read-only)
# ---------------------------------------------------------------------------

def _experiment_feedback(data_dir, *, followup_days: float = 14.0) -> dict:
    """open_from_briefs -> correlate_sale -> sweep. Every call is idempotent
    and never posts, spends, or calls an API. A failure is reported, not
    raised - experiment tracking must never break the loop."""
    try:
        from . import experiments
        return {
            "opened": experiments.open_from_briefs(data_dir)["opened"],
            "sales": experiments.correlate_sale(data_dir)["sale"],
            "swept": experiments.sweep(data_dir, followup_days=followup_days)["closed"],
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("experiment feedback failed: %s", exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# watch - bounded, resumable continuous driver (mirrors operator.run_continuous)
# ---------------------------------------------------------------------------

_SESSION_BLANK = {
    "started_at": None, "last_tick_at": None, "ticks": 0,
    "ended_at": None, "end_reason": None, "spend_baseline_usd": 0.0,
}


def _llm_spent(data_dir) -> float:
    from .llm_spend import LlmSpendLog
    return LlmSpendLog.load(
        Path(data_dir) / "llm_spend.json").summary()["total_cost_usd"]


def load_session(data_dir) -> tuple[dict, bool]:
    """(session, resumed). Resume an unfinished session; else a blank one."""
    sess = (load_state(data_dir).get("session") or {})
    if sess.get("started_at") and not sess.get("ended_at"):
        return {**_SESSION_BLANK, **sess}, True
    return dict(_SESSION_BLANK), False


def _save_session(data_dir, sess: dict) -> None:
    loop = load_state(data_dir)
    loop["session"] = sess
    _save_state(data_dir, loop)


def watch(data_dir, *, interval: float, max_ticks: int | None = None,
          max_runtime_s: float | None = None, max_spend_usd: float | None = None,
          fresh: bool = False, allow_discovery: bool = True, max_steps: int = 25,
          discovery_cooldown_hours: float = 6.0, followup_days: float = 14.0,
          on_tick=None, sleep_fn=None, clock_fn=None, now_fn=None) -> dict:
    """Tick `run()` to a fixed point, sleep, repeat - bounded and resumable.

    Never passes a human gate (it drives the existing supervisor, which
    stops at every gate). Ctrl-C exits cleanly with `end_reason` set. An
    unfinished session in data/revenue_loop.json resumes on restart. Each
    tick also runs the deterministic experiment feedback. Safe at EUR 0
    and with ANTHROPIC_API_KEY unset - the whole path is deterministic.
    """
    data_dir = Path(data_dir)
    sleep_fn = sleep_fn or time.sleep
    clock_fn = clock_fn or time.monotonic
    now_fn = now_fn or now_iso

    sess, resumed = load_session(data_dir)
    if fresh or not resumed:
        sess = dict(_SESSION_BLANK)
        sess["started_at"] = now_fn()
        sess["spend_baseline_usd"] = _llm_spent(data_dir)
    _save_session(data_dir, sess)

    start_clock = clock_fn()

    def _bound() -> str | None:
        if max_ticks is not None and sess["ticks"] >= max_ticks:
            return "max-ticks"
        if (max_runtime_s is not None
                and clock_fn() - start_clock >= max_runtime_s):
            return "max-runtime"
        if (max_spend_usd is not None
                and round(_llm_spent(data_dir) - sess["spend_baseline_usd"], 4)
                >= max_spend_usd):
            return "max-spend"
        return None

    reason: str | None = None
    try:
        while True:
            reason = _bound()
            if reason:
                break
            steps = run(data_dir, max_steps=max_steps,
                        allow_discovery=allow_discovery,
                        discovery_cooldown_hours=discovery_cooldown_hours,
                        followup_days=followup_days)
            fb = (steps[-1]["result"].get("feedback")
                  if steps else {}) or _experiment_feedback(
                      data_dir, followup_days=followup_days)
            sess["ticks"] += 1
            sess["last_tick_at"] = now_fn()
            _save_session(data_dir, sess)
            if on_tick is not None:
                on_tick(steps, fb)
            reason = _bound()
            if reason:
                break
            sleep_fn(interval)
    except (KeyboardInterrupt, SystemExit):
        reason = "interrupted"

    sess["ended_at"] = now_fn()
    sess["end_reason"] = reason or "stopped"
    _save_session(data_dir, sess)
    return sess
