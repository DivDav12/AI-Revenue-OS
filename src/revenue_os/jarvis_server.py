"""JARVIS - the localhost agent command console (a real control plane).

A separate app from `dashboard-serve`. The revenue dashboard stays
exactly as it was; this one is about *operating* the 24-agent fleet:

  * enable / disable any agent            (persisted -> agent_control.json)
  * global pause / resume                 (persisted -> agent_control.json)
  * run one deterministic agent now       (via agent_runner, EUR 0, no net)
  * run the one-cycle pipeline             (via pipeline.run_pipeline)
  * the 5 existing human gates             (delegated to dashboard_server)

Everything on screen is real persisted state - nothing is invented,
animated, or estimated. Every mutation goes through a tested domain
function; no lifecycle or financial logic lives in this file.

Safety model (identical to dashboard_server):
  * binds to loopback only - a non-loopback host is refused
  * every POST needs the per-process CSRF token + a same-origin check
  * no LLM call, no network fetch, no money, no PayPal, no outreach/email

Routes:
  GET  /            -> the console
  GET  /?partial=1  -> just <main> (the JS soft-refresh fetches this)
  POST /control     -> one allowlisted action, then 303 -> /
  POST /action      -> alias for the 5 human gates (kept for form reuse)
"""

from __future__ import annotations

import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

from . import agent_control, agent_runner, jarvis_intel, roster
from .agent_outputs import load_agent_outputs
from .dashboard_server import _load as _load_stores
from .dashboard_server import apply_action as _apply_gate
from .discovery_log import DiscoveryLog
from .jarvis_events import load_events, record_event
from .report import pipeline_report
from .revenuedashboard import _esc, _gate_form, _svg
from .store import now_iso
from .task_log import load_task_log

_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_MAX_BODY = 64 * 1024

_GATE_ACTIONS = ("approve", "reject", "outcome", "launch", "payment")
_CONTROL_ACTIONS = ("enable", "disable", "pause", "resume", "run",
                    "run-pipeline", "run-sweep",
                    "ack-gate", "reopen-gate", "outreach-status", "resolve-blocker",
                    "set-mode", "stop-job", "prepare-outreach", "refresh",
                    "run-autonomy", "approve-request", "deny-request",
                    "accept-opportunity", "abandon-opportunity", "run-worker")

_QUALIFIED = ("validated", "launched", "earning")

# --- background job: one pipeline / sweep run at a time --------------------
_JOB = {"running": False, "what": "", "started": "", "stop_requested": False}
_JOB_LOCK = threading.Lock()


def _job_state() -> dict:
    with _JOB_LOCK:
        return dict(_JOB)


def _stop_requested() -> bool:
    with _JOB_LOCK:
        return bool(_JOB.get("stop_requested"))


def _request_stop() -> str:
    with _JOB_LOCK:
        if not _JOB["running"]:
            return "error: no job is running"
        _JOB["stop_requested"] = True
        what = _JOB["what"]
    return (f"ok: STOP REQUESTED for the {what}. The current step finishes "
            "(no step is interrupted), then it halts and persists.")


def _start_job(what: str, target, data_dir, *args) -> str:
    with _JOB_LOCK:
        if _JOB["running"]:
            return f"error: {_JOB['what']} already running - watch the bars"
        _JOB.update(running=True, what=what, started=now_iso(), stop_requested=False)
    record_event(data_dir, "job", f"{what} started")

    def _run():
        outcome = "finished"
        try:
            target(data_dir, *args)
        except Exception:                       # never let the thread die loud
            logger.exception("JARVIS background job %s failed", what)
            outcome = "failed"
        finally:
            with _JOB_LOCK:
                stopped = _JOB.get("stop_requested")
                _JOB.update(running=False, what="", started="", stop_requested=False)
            record_event(data_dir, "job",
                         f"{what} {'stopped by operator' if stopped else outcome}")

    threading.Thread(target=_run, name=f"jarvis-{what}", daemon=True).start()
    return f"ok: {what} started - the bars update every 4s"

# Capabilities JARVIS can run standalone: deterministic, EUR 0, and their
# input can be built entirely from persisted state. Everything else is
# reachable through the pipeline or the CLI (the Run button is disabled
# with a reason).
RUNNABLE_HERE = frozenset({
    "select", "find_suppliers", "package_deliverable", "design_assets",
    "build_store", "quality_check", "analyze_trends", "analyze_revenue",
    # human-gated agents JARVIS can run to PRODUCE THE DRAFT/SPEC only
    # (tagged human_gate_required; no code shipped, no ad money spent,
    #  no campaign changed, no budget allocated, no post):
    "develop", "automate", "run_ads", "draft_outreach",
    "optimize_campaigns", "allocate_budget",
})

# short one-line purpose per agent, shown on the card
_DESCRIPTION = {
    "market_scanner": "pulls raw opportunity signals from a source",
    "opportunity_finder": "ranks + shortlists the discovered opportunities",
    "product_researcher": "due-diligence on a shortlisted opportunity (LLM)",
    "trend_hunter": "summarises recurring themes across the corpus",
    "competitor_analyzer": "reads the competitive landscape (LLM)",
    "supplier_finder": "sourcing feasibility - never contacts a supplier",
    "content_creator": "assembles the self-contained launch page",
    "copywriter": "drafts launch copy (LLM)",
    "designer": "visual asset + page-layout spec",
    "store_builder": "storefront / checkout page spec (draft only)",
    "developer": "deterministic implementation plan (draft only)",
    "automation_engineer": "workflow graph spec (draft only)",
    "prospect_scout": "finds public 'how do I get customers' posts",
    "opportunity_scorer": "scores + ranks scraped prospects",
    "outreach_drafter": "drafts one reply - a human posts it",
    "ads_manager": "campaign spec - no ad money is spent",
    "campaign_optimizer": "optimization recommendation - no campaign changed",
    "budget_allocator": "budget split recommendation - no money allocated",
    "sales_tracker": "sales ledger from supplied payment events",
    "profit_master": "margin arithmetic from supplied figures",
    "revenue_analyst": "portfolio ROI read-out",
    "customer_support": "drafts a support reply - auto-send is off",
    "review_manager": "aggregates supplied customer feedback",
    "quality_control": "deterministic pre-launch QA gate",
}

# Why each human-gated agent will never auto-execute - shown verbatim.
_HUMAN_WHY = {
    "store_builder": "publishes a real storefront / checkout page - a human "
                     "reviews the spec and runs the deploy",
    "developer": "writes real code and config - a human reviews it before "
                 "anything ships",
    "automation_engineer": "wires live automation - a human approves the "
                           "workflow before it runs",
    "outreach_drafter": "drafts a reply you must edit and post yourself; the "
                        "system never posts, DMs, or emails",
    "ads_manager": "spends real advertising money - a human launches and funds "
                   "every campaign",
    "campaign_optimizer": "changes live campaigns - needs real metrics and a "
                          "human decision",
    "budget_allocator": "allocates real budget - a human approves every number; "
                        "the reserved growth capital stays locked until a sale",
}

_NOT_RUNNABLE_WHY = {
    "discover": "needs a live search source (network) - run `revenue_os run`",
    "scout_prospects": "needs a live search source (network) - run "
                       "`revenue_os discover-free`",
    "research": "LLM-only - costs money; run it from the CLI with a budget",
    "analyze_competition": "LLM-only - costs money; run it from the CLI with a "
                           "budget",
    "write_copy": "LLM-only - costs money; run it from the CLI with a budget",
    "track_sales": "needs real funnel / payment-event input from you",
    "manage_profit": "needs real cost / revenue figures from you",
    "support_customers": "needs a real customer intake message",
    "manage_reviews": "needs real customer feedback rows",
    "optimize_campaigns": "needs real campaign metrics - only exist after ads run",
    "allocate_budget": "needs a real budget figure you approve - not synthesised",
    "score_prospects": "needs a batch of scraped prospect records",
}

# What "handled" means for each human-gated agent - the real out-of-band
# step you confirm with "Mark handled".
_HUMAN_NEXT = {
    "store_builder": "build + publish the real checkout page: "
                     "`revenue_os build-checkout <cand> --price ...` then `deploy-checkout`",
    "developer": "implement the components from the spec in your codebase",
    "automation_engineer": "wire the workflow in your automation tool (Make / n8n / cron)",
    "outreach_drafter": "post the reply yourself, then use the buttons below to log it",
    "ads_manager": "create + fund the campaign in the ad platform yourself",
    "campaign_optimizer": "apply the changes in the ad platform once real metrics exist",
    "budget_allocator": "approve the split and set real per-candidate budgets "
                        "(`revenue_os budget <cand> <amount>`)",
}


# ---------------------------------------------------------------------------
# domain: run one agent
# ---------------------------------------------------------------------------

def _qualified_candidate(store):
    cands = [c for c in store.all() if c.status in _QUALIFIED and c.offer]
    if not cands:
        return None
    return max(cands, key=lambda c: float(c.total or 0.0))


def _jarvis_payload(data_dir: Path, capability: str, store, revenue_ledger,
                    spend_ledger, outs_flat: dict):
    """(payload, why_not). payload is None when it cannot be built."""
    from . import pipeline as _pipeline

    if capability in {"select", "find_suppliers", "package_deliverable",
                      "design_assets", "build_store", "quality_check"}:
        cand = _qualified_candidate(store)
        if cand is None:
            return None, ("no qualified candidate - approve one through to "
                          "validated / launched / earning with an offer first")
        return _pipeline._payload(capability, cand, outs_flat), ""

    if capability == "analyze_trends":
        dlog = DiscoveryLog.load(data_dir / "discovery_runs.json")
        return {
            "candidates": [
                {"name": c.name, "description": c.description,
                 "source": c.source, "total": c.total}
                for c in store.all()
            ],
            "runs": len(dlog.entries()),
        }, ""

    if capability == "analyze_revenue":
        report = pipeline_report(store, revenue_ledger, spend_ledger)
        return {
            "roi": report["roi"],
            "outcomes": report.get("outcomes", {}),
            "candidates": report.get("candidates", []),
        }, ""

    # --- human-gated agents: build a DRAFT-ONLY payload ------------------
    if capability in ("develop", "automate", "run_ads", "optimize_campaigns",
                      "allocate_budget"):
        cand = _qualified_candidate(store)
        if cand is None:
            return None, ("no qualified candidate - approve one to "
                          "validated / launched / earning with an offer first")
        offer = dict(cand.offer or {})
        pkg = outs_flat.get("package_deliverable") or {}
        if capability == "develop":
            spec_in = (outs_flat.get("build_store")
                       or {"candidate": cand.name, "offer": offer,
                           "note": "draft requested from JARVIS"})
            return {"build_specification": dict(spec_in)}, ""
        if capability == "automate":
            steps = [v for v in outs_flat.values() if isinstance(v, dict) and v]
            if not steps:
                return None, ("run the build agents first - Automation Engineer "
                              "wires their outputs together")
            return {"agent_outputs": steps}, ""
        if capability == "run_ads":
            return {"offer": offer,
                    "landing_page": str(pkg.get("landing_html", "")),
                    "positioning": str(offer.get("positioning", ""))}, ""
        if capability == "optimize_campaigns":
            # no live campaigns / metrics exist -> the agent returns a
            # "nothing to optimize yet" recommendation. No campaign is touched.
            return {"campaign_metrics": [],
                    "campaign_plan": dict(outs_flat.get("run_ads") or {})}, ""
        # allocate_budget: recommend a SPLIT of the pre-sale cap across the
        # drafted campaign(s). No money is moved; growth capital stays locked.
        ads = outs_flat.get("run_ads") or {}
        opts = ads.get("campaigns") or ads.get("campaign_options") or []
        if not isinstance(opts, list) or not opts:
            opts = [{"name": "primary", "channel": "tbd"}]
        return {"available_budget": 0.0, "campaign_options": list(opts),
                "note": "JARVIS draft - budget 0 until a human approves a figure"}, ""

    if capability == "draft_outreach":
        leads = _json_leads(data_dir)
        if not leads:
            return None, ("no acquisition leads yet - run discovery from the CLI "
                          "(`revenue_os discover-free`)")
        from .outreach import resolve_checkout_url
        return {"lead": leads[0], "checkout_url": resolve_checkout_url(store)}, ""

    return None, _NOT_RUNNABLE_WHY.get(capability, "not runnable from JARVIS")


def _json_leads(data_dir: Path) -> list:
    p = Path(data_dir) / "acquisition.json"
    if not p.exists():
        return []
    try:
        import json
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = raw if isinstance(raw, list) else []
    rows = [r for r in rows if isinstance(r, dict) and r.get("lead_id")]
    rows.sort(key=lambda r: float(r.get("final_score", r.get("relevance_score", 0)) or 0),
              reverse=True)
    return rows


def run_one_agent(data_dir, agent_id: str, actor: str = "jarvis") -> str:
    data_dir = Path(data_dir)
    spec = roster.get(agent_id)
    if spec is None:
        return f"error: unknown agent {agent_id!r}"
    if spec.status != "live":
        return f"error: {spec.name} is not live"

    ok, reason = agent_control.check_runnable(data_dir, spec.capability)
    if not ok:
        return f"error: {reason}"
    if spec.capability not in RUNNABLE_HERE:
        why = _NOT_RUNNABLE_WHY.get(spec.capability, "runs through the pipeline")
        return f"error: {spec.name} cannot be run from JARVIS - {why}"

    try:
        store, revenue_ledger, spend_ledger = _load_stores(data_dir)
        outs_flat = {
            k: (v.get("output") if isinstance(v, dict) else {})
            for k, v in load_agent_outputs(data_dir).all().items()
        }
        payload, why_not = _jarvis_payload(
            data_dir, spec.capability, store, revenue_ledger, spend_ledger, outs_flat
        )
        if payload is None:
            return f"error: {why_not}"
        res = agent_runner.run_agent(
            data_dir, spec.capability, payload,
            objective=f"JARVIS manual run: {spec.name}",
        )
    except (ValueError, FileNotFoundError) as exc:
        return f"error: {exc}"

    if res.status != "ok":
        return f"error: {spec.name} - {res.error}"
    tail = " (draft/spec only - the human gate still stands)" if spec.gate == "human" else ""
    return f"ok: {spec.name} ran{tail}"


# seconds paused after each pipeline step in a background run, so the
# live bars are watchable (the steps themselves take milliseconds).
_STEP_DELAY = 0.7


def _pipeline_job(data_dir, name, restart) -> None:
    """Background target: one pipeline cycle, deploy skipped (publishing
    stays a human/CLI action). Honours a STOP request between steps."""
    from .pipeline import run_pipeline

    run_pipeline(data_dir, name, restart=restart, skip_deploy=True,
                 step_delay=_STEP_DELAY, should_stop=_stop_requested)


def _sweep_job(data_dir, name, restart, actor) -> None:
    """Background target: full fleet sweep - the pipeline, then the two
    deterministic analysts that read the fresh state. Stops cleanly on request."""
    _pipeline_job(data_dir, name, restart)
    if _stop_requested():
        return
    for cap in ("analyze_trends", "analyze_revenue"):
        if _stop_requested():
            return
        spec = roster.by_capability(cap)
        if spec is not None:
            run_one_agent(data_dir, spec.id, actor)


def _autonomy_job(data_dir, cycles: int) -> None:
    """Background target: run N autonomous revenue cycles, honouring STOP
    and the global pause. EUR 0, no LLM, no money, no external send.
    After each cycle it also drains the ExecutionTask queue (bounded) so an
    accepted opportunity's chain progresses without a separate button."""
    from . import autonomy, worker
    from .agent_control import load_agent_control

    for _ in range(cycles):
        if _stop_requested() or load_agent_control(data_dir).is_paused():
            return
        autonomy.run_cycle(data_dir)
        try:
            worker.run_worker(data_dir, max_ticks=25)
        except Exception:
            logger.exception("worker drain after autonomy cycle failed")


def _worker_job(data_dir, max_ticks: int) -> None:
    """Background target: drain the ExecutionTask queue once. Honours STOP
    and the global pause. EUR 0, no LLM, no money, no external send."""
    from . import worker
    from .agent_control import load_agent_control

    if _stop_requested() or load_agent_control(data_dir).is_paused():
        return
    worker.run_worker(data_dir, max_ticks=max(1, int(max_ticks)))


# ---------------------------------------------------------------------------
# domain: apply one action
# ---------------------------------------------------------------------------

def apply_control(data_dir, actor: str, form: dict) -> str:
    """Run one allowlisted action, log it to the activity feed, return a
    flash string. Never raises."""
    action = (form.get("action") or [""])[0]
    msg = _apply_control_inner(data_dir, actor, form, action)
    if action and action != "refresh":
        record_event(data_dir, "action", f"{action}: {msg}", actor=actor)
    return msg


def _apply_control_inner(data_dir, actor: str, form: dict, action: str) -> str:
    if action in _GATE_ACTIONS:
        return _apply_gate(Path(data_dir), actor, form)

    if action not in _CONTROL_ACTIONS:
        return f"error: unknown action {action!r}"

    if action == "refresh":
        return ""

    if action == "set-mode":
        mode = (form.get("mode") or [""])[0]
        try:
            ctrl = agent_control.load_agent_control(data_dir)
            ctrl.set_mode(mode, by=actor)
            ctrl.save()
            return f"ok: fleet mode -> {ctrl.mode.upper()}"
        except ValueError as exc:
            return f"error: {exc}"

    if action == "stop-job":
        return _request_stop()

    if action == "run-autonomy":
        ctrl = agent_control.load_agent_control(data_dir)
        if ctrl.is_paused():
            return "error: fleet is paused - resume before running the loop"
        cycles = max(1, min(10, int((form.get("cycles") or ["1"])[0] or 1)))
        return _start_job("autonomy loop", _autonomy_job, data_dir, cycles)

    if action == "run-worker":
        ctrl = agent_control.load_agent_control(data_dir)
        if ctrl.is_paused():
            return "error: fleet is paused - resume before draining the queue"
        ticks = max(1, min(200, int((form.get("max_ticks") or ["50"])[0] or 50)))
        return _start_job("worker drain", _worker_job, data_dir, ticks)

    if action in ("accept-opportunity", "abandon-opportunity"):
        from . import acceptance
        oid = (form.get("opp") or [""])[0]
        try:
            if action == "accept-opportunity":
                r = acceptance.accept_opportunity(data_dir, oid, actor=actor)
                n = len(r["created"])
                return (f"ok: accepted {oid[:12]} -> {r['state']}; "
                        f"{n} task(s) queued"
                        + (f", {len(r['reused'])} already existed"
                           if r["reused"] else "")
                        + " - hit 'Run worker' or enable AUTONOMOUS to execute")
            r = acceptance.abandon_opportunity(
                data_dir, oid, actor=actor,
                reason=(form.get("reason") or [""])[0])
            return (f"ok: abandoned {oid[:12]} -> {r['state']}; "
                    f"{len(r['cancelled'])} task(s) cancelled")
        except acceptance.AcceptanceError as exc:
            return f"error: {exc}"

    if action in ("approve-request", "deny-request"):
        from .approvals import load_approvals
        rid = (form.get("id") or [""])[0]
        appr = load_approvals(data_dir)
        if appr.get(rid) is None:
            return f"error: unknown approval request {rid!r}"
        decision = "approved" if action == "approve-request" else "denied"
        try:
            r = appr.decide(rid, decision, by=actor,
                            note=(form.get("note") or [""])[0])
            appr.save()
        except ValueError as exc:
            return f"error: {exc}"
        return (f"ok: {r['kind']} request {rid[:8]} {decision} - "
                + ("the fleet may now perform the "
                   f"{r['kind']} step OUTSIDE autonomous mode"
                   if decision == "approved"
                   else "the fleet will stop re-filing it"))

    if action == "prepare-outreach":
        lead_id = (form.get("lead_id") or [""])[0]
        leads = _json_leads(data_dir)
        match = [l for l in leads if str(l.get("lead_id", "")).startswith(lead_id)]
        if len(match) != 1:
            return f"error: {'no' if not match else 'multiple'} lead(s) match {lead_id!r}"
        from .outreach import OutreachStore, resolve_checkout_url
        store, _, _ = _load_stores(Path(data_dir))
        ok, reason = agent_control.check_runnable(data_dir, "draft_outreach")
        if not ok:
            return f"error: {reason}"
        res = agent_runner.run_agent(
            data_dir, "draft_outreach",
            {"lead": match[0], "checkout_url": resolve_checkout_url(store)},
            objective=f"JARVIS: prepare outreach for {match[0].get('lead_id')}")
        if res.status != "ok":
            return f"error: Outreach Drafter - {res.error}"
        return (f"ok: outreach draft prepared for {match[0].get('lead_id', '')[:8]} "
                "- review it and post it yourself")

    try:
        if action in ("enable", "disable"):
            aid = (form.get("agent") or [""])[0]
            ctrl = agent_control.load_agent_control(data_dir)
            ctrl.set_agent(aid, action == "enable", by=actor,
                           note=(form.get("note") or [""])[0])
            ctrl.save()
            spec = roster.get(aid)
            return f"ok: {spec.name if spec else aid} {action}d"

        if action in ("pause", "resume"):
            ctrl = agent_control.load_agent_control(data_dir)
            ctrl.set_paused(action == "pause", by=actor,
                            reason=(form.get("reason") or [""])[0])
            ctrl.save()
            return ("ok: ALL agents PAUSED" if action == "pause"
                    else "ok: control plane RESUMED")

        if action == "run":
            return run_one_agent(data_dir, (form.get("agent") or [""])[0], actor)

        if action in ("run-pipeline", "run-sweep"):
            from .pipeline import run_pipeline

            ctrl = agent_control.load_agent_control(data_dir)
            if ctrl.is_paused():
                return (f"error: control plane is paused - "
                        f"{ctrl.paused_reason or 'resume first'}")
            store, _, _ = _load_stores(Path(data_dir))
            name = (form.get("candidate") or [""])[0]
            if not name:
                cand = _qualified_candidate(store)
                name = cand.name if cand else ""
            if not name:
                return ("error: no candidate given and none is qualified "
                        "(need validated / launched / earning + an offer)")
            restart = (form.get("restart") or [""])[0] in ("1", "true", "on", "yes")

            # async (the UI default): spawn a thread so the bars move live.
            # sync path is kept for the CLI / tests.
            if (form.get("mode") or [""])[0] == "async":
                if action == "run-sweep":
                    return _start_job("fleet sweep", _sweep_job, data_dir, name,
                                      restart, actor)
                return _start_job("pipeline", _pipeline_job, data_dir, name, restart)
            rep = run_pipeline(data_dir, name, restart=restart, skip_deploy=True)
            if action == "run-sweep":
                for cap in ("analyze_trends", "analyze_revenue"):
                    run_one_agent(data_dir, roster.by_capability(cap).id, actor)
            return f"ok: {action} {rep.get('candidate')} -> {rep.get('status')}"

        if action in ("ack-gate", "reopen-gate"):
            aid = (form.get("agent") or [""])[0]
            spec = roster.get(aid)
            if spec is None:
                return f"error: unknown agent {aid!r}"
            ctrl = agent_control.load_agent_control(data_dir)
            if action == "reopen-gate":
                ctrl.reopen_gate(aid)
                ctrl.save()
                return f"ok: {spec.name} gate re-opened"
            out = load_agent_outputs(data_dir).get(spec.capability)
            ts = (out or {}).get("ts", "")
            ctrl.acknowledge_gate(aid, ts, by=actor,
                                  note=(form.get("note") or [""])[0])
            ctrl.save()
            return f"ok: {spec.name} gate marked handled by {actor}"

        if action == "outreach-status":
            status = (form.get("status") or [""])[0]
            if status not in ("posted", "skipped"):
                return "error: outreach status must be posted or skipped"
            lead_id = (form.get("lead_id") or [""])[0]
            return _record_outreach_status(data_dir, lead_id, status,
                                           (form.get("reason") or [""])[0])

        if action == "resolve-blocker":
            from .blockers import load_blockers
            bid = (form.get("id") or [""])[0]
            bs = load_blockers(data_dir)
            if bs.get(bid) is None:
                return f"error: unknown blocker {bid!r}"
            bs.resolve(bid)
            bs.save()
            return f"ok: blocker {bid} resolved"
    except (ValueError, FileNotFoundError) as exc:
        return f"error: {exc}"
    return f"error: unhandled action {action!r}"


def _record_outreach_status(data_dir, lead_id: str, status: str, reason: str) -> str:
    """Mirror `revenue_os outreach-status`: record what the human did with
    a drafted brief and keep the experiment ledger in step. Posts nothing."""
    from . import experiments
    from .outreach import OutreachStore

    data_dir = Path(data_dir)
    store = OutreachStore.load(data_dir / "outreach.json")
    matches = [b for b in store.all()
               if str(b.get("lead_id", "")).startswith(lead_id)]
    if len(matches) != 1:
        return (f"error: {'no' if not matches else 'multiple'} brief(s) match "
                f"id {lead_id!r}")
    lid = matches[0]["lead_id"]
    store.set_status(lid, status, reason=reason)
    store.save()
    try:
        experiments.open_from_briefs(data_dir)
        experiments.advance(data_dir, lid, status, note="via JARVIS", reason=reason)
    except ValueError:
        pass
    return f"ok: outreach {lid[:8]} marked {status} (you posted it, not the system)"


# ---------------------------------------------------------------------------
# read model
# ---------------------------------------------------------------------------

def _ts_short(ts: object) -> str:
    return str(ts or "")[:16].replace("T", " ")


def _why_waiting(spec, out_entry, pipe: dict, gate_text: str, ctrl=None) -> str:
    gate = pipe.get("human_gate") if isinstance(pipe.get("human_gate"), dict) else {}
    steps = pipe.get("steps") if isinstance(pipe.get("steps"), dict) else {}
    step = steps.get(spec.capability) if isinstance(steps.get(spec.capability), dict) else {}
    out = out_entry.get("output") if isinstance(out_entry, dict) else None
    out_ts = out_entry.get("ts", "") if isinstance(out_entry, dict) else ""

    # a human has already marked this gate handled -> nothing waiting
    if (spec.gate == "human" and ctrl is not None
            and ctrl.gate_acknowledged(spec.id, out_ts)):
        return ""

    if step.get("status") == "blocked":
        return str(step.get("reason") or gate.get("reason") or "pipeline step blocked")
    if step.get("status") == "failed":
        return f"last pipeline step failed: {step.get('reason', '')}"
    if isinstance(out, dict) and out.get("human_gate_required") is True:
        return "its last output is tagged human_gate_required - your decision moves it"
    if spec.gate == "human":
        base = _HUMAN_WHY.get(
            spec.id, "this action crosses a money or legal line - a human must approve"
        )
        if gate_text and spec.id in gate_text:
            return f"{base}. The pipeline is stopped here: {gate.get('reason', '')}"
        return base
    return ""


_STEP_DONE = ("ok", "skipped")
_STEP_BAD = ("blocked", "failed")


def _agent_progress(spec, out_entry, outs: dict, pipe: dict,
                    have_candidates: bool, have_leads: bool,
                    gate_state: str = "") -> tuple[int, str, str]:
    """(percent 0-100, label, kind). kind in ok|run|bad|idle - drives the
    bar colour. Every value is derived from persisted state, never faked.
    gate_state: "" | "open" (unacked human-gate draft) | "handled"."""
    from .pipeline import STEP_ORDER

    steps = pipe.get("steps") if isinstance(pipe.get("steps"), dict) else {}
    pstatus = pipe.get("status")
    cap = spec.capability

    # 1) part of a pipeline run for the current candidate -> live step state
    if cap in STEP_ORDER and pipe.get("candidate"):
        s = (steps.get(cap) or {}).get("status")
        if s in _STEP_DONE:
            return 100, s, "ok"
        if s == "running":
            return 66, "running", "run"     # animated: genuinely in flight
        if s in _STEP_BAD:
            return 100, s, "bad"
        # no status yet for this step
        if pstatus == "running":
            return 8, "queued", "run"
        if pstatus in ("prepared", "blocked", "failed"):
            return 0, "not reached", "idle"
        return 0, "pending", "idle"

    # 2) ran standalone (or in a past cycle) and its output is on disk
    if isinstance(out_entry, dict):
        if gate_state == "open":
            return 100, "draft ready - your call", "idle"
        if gate_state == "handled":
            return 100, "handled by you", "ok"
        return 100, "complete", "ok"

    # 3) otherwise: readiness = how many of its inputs already exist
    deps = spec.depends_on
    if not deps:
        return 0, "standby", "idle"
    ready = 0
    for d in deps:
        ds = roster.get(d)
        if ds is None:
            continue
        if ds.capability in outs:
            ready += 1
        elif d == "market_scanner" and have_candidates:
            ready += 1
        elif d == "prospect_scout" and have_leads:
            ready += 1
    pct = round(100 * ready / len(deps))
    # static bar - "armed" (green) when every input is in, else a partial
    # neutral fill. Never animated: readiness is a resting state, not work.
    kind = "ok" if pct >= 100 else "idle"
    label = "armed - inputs ready" if pct >= 100 else f"{ready}/{len(deps)} inputs ready"
    return pct, label, kind


def jarvis_snapshot(data_dir) -> dict:
    """Everything the console renders - all of it from disk."""
    data_dir = Path(data_dir)
    store, revenue_ledger, spend_ledger = _load_stores(data_dir)
    ctrl = agent_control.load_agent_control(data_dir)

    outs = load_agent_outputs(data_dir).all()
    tasks_by_agent: dict[str, list] = {}
    try:
        for e in load_task_log(data_dir).entries():
            tasks_by_agent.setdefault(str(e.get("agent") or ""), []).append(e)
    except Exception:
        pass

    def _json(name):
        p = data_dir / name
        if not p.exists():
            return None
        try:
            import json
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    pipe = _json("pipeline.json") or {}
    gate = pipe.get("human_gate") if isinstance(pipe.get("human_gate"), dict) else {}
    gate_text = " ".join(str(x) for x in (
        list(gate.get("human_gated_next") or []) + list(gate.get("blocking_issues") or [])
    ))

    report = pipeline_report(store, revenue_ledger, spend_ledger,
                             DiscoveryLog.load(data_dir / "discovery_runs.json"))

    try:
        from .budget import status as _budget_status
        budget = _budget_status(data_dir)
    except Exception:
        budget = {}
    try:
        from .blockers import load_blockers
        blockers = [b for b in load_blockers(data_dir).all()
                    if b.get("status") == "open"]
    except Exception:
        blockers = []

    steps = pipe.get("steps") if isinstance(pipe.get("steps"), dict) else {}
    outs_flat = {k: (v.get("output") if isinstance(v, dict) else {})
                 for k, v in outs.items()}
    have_candidates = bool(store.all())
    acq = _json("acquisition.json")
    have_leads = bool(acq) if isinstance(acq, list) else False

    agents = []
    for spec in roster.AGENTS:
        tl = sorted(tasks_by_agent.get(spec.id, []), key=lambda e: str(e.get("ts", "")))
        out_entry = outs.get(spec.capability)
        enabled = ctrl.is_enabled(spec.id)
        step = steps.get(spec.capability) if isinstance(steps.get(spec.capability), dict) else {}
        why = _why_waiting(spec, out_entry, pipe, gate_text, ctrl)
        out_ts = out_entry.get("ts", "") if isinstance(out_entry, dict) else ""
        gate_ack = bool(spec.gate == "human"
                        and ctrl.gate_acknowledged(spec.id, out_ts))
        ack_info = ctrl.gate_ack_info(spec.id) if gate_ack else {}
        has_draft = isinstance(out_entry, dict)

        step_status = step.get("status")
        if spec.status != "live":
            state = "planned"
        elif ctrl.is_paused():
            state = "paused"
        elif not enabled:
            state = "disabled"
        elif step_status == "running":
            state = "running"
        elif step_status in ("blocked", "failed"):
            state = "failed" if step_status == "failed" else "blocked"
        elif why:
            state = "waiting"
        elif gate_ack:
            state = "handled"
        elif spec.gate == "human":
            state = "human"
        elif has_draft or step_status in ("ok", "skipped"):
            state = "completed"
        else:
            state = "idle"

        last_result = ""
        if tl:
            n = tl[-1]
            last_result = str(n.get("status") or n.get("error") or "ok")
            last = f"{_ts_short(n.get('ts'))} · {n.get('capability') or ''}"
        elif isinstance(out_entry, dict):
            o = out_entry.get("output") or {}
            last_result = "human_gate_required" if o.get("human_gate_required") else "ok"
            last = f"{_ts_short(out_entry.get('ts'))} · output persisted"
        else:
            last = ""

        deps = [{"id": d, "name": (roster.get(d).name if roster.get(d) else d),
                 "met": (roster.get(d) is not None
                         and roster.get(d).capability in outs_flat)}
                for d in spec.depends_on]

        runnable_here = spec.capability in RUNNABLE_HERE
        run_ok, run_reason = ctrl.runnable(spec.capability)
        gate_state = ("handled" if gate_ack
                      else ("open" if (spec.gate == "human" and has_draft) else ""))
        pct, plabel, pkind = _agent_progress(
            spec, out_entry, outs_flat, pipe, have_candidates, have_leads, gate_state)
        if state in ("disabled", "paused", "planned"):
            pkind = "idle"
        agents.append({
            "id": spec.id, "name": spec.name, "cluster": spec.cluster,
            "role": spec.role, "capability": spec.capability, "gate": spec.gate,
            "description": _DESCRIPTION.get(spec.id, spec.role),
            "dependencies": deps,
            "enabled": enabled, "state": state, "runs": len(tl),
            "last": last, "last_result": last_result, "why_waiting": why,
            "progress": pct, "progress_label": plabel, "progress_kind": pkind,
            "runnable_here": runnable_here,
            "can_run_now": bool(runnable_here and run_ok),
            "run_blocked_reason": ("" if run_ok else run_reason) if runnable_here
            else _NOT_RUNNABLE_WHY.get(spec.capability, "runs via the pipeline"),
            "human_gated": spec.gate == "human",
            "has_draft": has_draft,
            "gate_acknowledged": gate_ack,
            "gate_ack_by": ack_info.get("ack_by", ""),
            "gate_ack_at": _ts_short(ack_info.get("ack_at", "")),
            "gate_ack_note": ack_info.get("ack_note", ""),
            "next_step_hint": _HUMAN_NEXT.get(spec.id, ""),
            "pipeline_step": (spec.capability
                             if spec.capability in _pipeline_step_order() else None),
            "history": [
                {"ts": _ts_short(e.get("ts")),
                 "capability": e.get("capability", ""),
                 "status": e.get("status", ""),
                 "objective": (e.get("objective") or "")[:80]}
                for e in tl[-6:][::-1]
            ],
        })

    order = list(_pipeline_step_order())
    pipe_steps = [{"cap": c,
                   "agent": (roster.by_capability(c).name
                             if roster.by_capability(c) else c),
                   **(steps.get(c) or {"status": "pending"})} for c in order]
    done = sum(1 for s in pipe_steps if s["status"] in ("ok", "skipped"))
    bad = sum(1 for s in pipe_steps if s["status"] in ("blocked", "failed"))
    running_step = next((s["cap"] for s in pipe_steps if s["status"] == "running"), None)
    next_pending = next((s["cap"] for s in pipe_steps
                         if s["status"] not in ("ok", "skipped")), None)
    job = _job_state()

    def _c(*sts):
        return sum(1 for a in agents if a["state"] in sts)
    counts = {
        "total": len(agents),
        "live": sum(1 for a in agents if roster.get(a["id"]).status == "live"),
        "running": _c("running"),
        "idle": _c("idle"),
        "completed": _c("completed", "handled"),
        "waiting_human": _c("waiting", "human"),
        "blocked": _c("blocked") + len(blockers),
        "failed": _c("failed"),
        "disabled": _c("disabled"),
        "paused": _c("paused"),
        # kept for backwards-compat with earlier callers/tests
        "waiting": _c("waiting", "human"),
    }

    briefs = _json("outreach.json")
    outreach = [
        {"lead_id": b.get("lead_id", ""),
         "status": b.get("status", ""),
         "title": (b.get("brief") or {}).get("title", "")
                  or (b.get("brief") or {}).get("url", ""),
         "platform": (b.get("brief") or {}).get("platform", "")}
        for b in (briefs if isinstance(briefs, list) else [])
        if isinstance(b, dict) and b.get("status") in ("draft", "approved")
    ]
    open_gates = (len(report.get("action_queue", [])) + len(outreach)
                  + len(blockers) + counts["waiting_human"])

    jstatus = ("PAUSED" if ctrl.is_paused()
               else "WORKING" if job.get("running")
               else "HOLDING" if (pipe.get("status") == "prepared" or open_gates)
               else "ONLINE")

    llm_summary = _llm_spend_summary(data_dir)
    fin = jarvis_intel.financial_safety(
        budget=budget, blockers=blockers, llm_spend_summary=llm_summary,
        revenue_eur=report["totals"]["grand_revenue"])

    qual = _qualified_candidate(store)
    cand_dict = ({"name": qual.name, "status": qual.status} if qual else None)
    ds = _deploy_state(data_dir, qual.name if qual else "")
    intake_rows = _json("intake.json") or _json("buyer_intake.json") or []
    deliver_dir = data_dir / "deliverables"
    delivered = sum(1 for p in deliver_dir.glob("*/*.pdf")) if deliver_dir.exists() else 0
    rev_pipe = jarvis_intel.revenue_pipeline(
        candidate=cand_dict,
        checkout_built=ds.get("built", False),
        checkout_deployed=ds.get("deployed", False),
        paypal_blocked=fin["paypal"]["state"] == "BLOCKED",
        intake_count=len(intake_rows) if isinstance(intake_rows, list) else 0,
        plan_count=sum(1 for r in intake_rows
                       if isinstance(r, dict) and r.get("plan"))
        if isinstance(intake_rows, list) else 0,
        delivered_count=delivered,
        revenue_eur=report["totals"]["grand_revenue"],
        leads=len(acq) if isinstance(acq, list) else 0,
        outreach_ready=len(outreach))

    acq_view = jarvis_intel.acquisition_view(
        leads=acq if isinstance(acq, list) else [],
        briefs=briefs if isinstance(briefs, list) else [],
        last_discovery=(DiscoveryLog.load(data_dir / "discovery_runs.json").latest()
                        if (data_dir / "discovery_runs.json").exists() else None))

    snap = {
        "open_gates": open_gates,
        "outreach": outreach,
        "generated_at": now_iso(),
        "status": jstatus,
        "mode": ctrl.mode,
        "paused": ctrl.is_paused(),
        "paused_reason": ctrl.paused_reason,
        "counts": counts,
        "agents": agents,
        "budget": budget,
        "revenue_eur": report["totals"]["grand_revenue"],
        "blockers": blockers,
        "job": job,
        "financial": fin,
        "revenue_pipeline": rev_pipe,
        "acquisition": acq_view,
        "events": load_events(data_dir).recent(40),
        "pipeline": {"candidate": pipe.get("candidate"), "status": pipe.get("status"),
                     "steps": pipe_steps,
                     "done": done, "bad": bad, "total": len(order),
                     "pct": round(100 * done / len(order)) if order else 0,
                     "current_step": running_step or next_pending,
                     "error": pipe.get("error"),
                     "human_gate": gate},
        "action_queue": report.get("action_queue", []),
        "profit_scale": jarvis_intel.profit_scale(
            revenue_ledger.entries(), spend_ledger.entries()),
    }
    try:
        from . import autonomy as _auto
        snap["autonomy"] = _auto.snapshot(data_dir)
    except Exception:
        snap["autonomy"] = {"state": {}, "opportunity_counts": {}, "board": {},
                            "pending": {"money": [], "identity": [], "legal": []},
                            "approval_counts": {}}
    try:
        from . import acceptance as _acc
        snap["execution"] = _acc.execution_view(data_dir)
    except Exception:
        snap["execution"] = []
    try:
        from .llm_gateway import status as _llm_status
        snap["llm"] = _llm_status(data_dir)
    except Exception:
        snap["llm"] = {}
    snap["recommendations"] = jarvis_intel.recommendations(snap)
    snap["human_actions"] = jarvis_intel.human_actions(snap)
    return snap


def _llm_spend_summary(data_dir) -> dict:
    try:
        from .llm_spend import LlmSpendLog
        return LlmSpendLog.load(Path(data_dir) / "llm_spend.json").summary()
    except Exception:
        return {}


def _deploy_state(data_dir, candidate: str) -> dict:
    if not candidate:
        return {}
    try:
        from .deploy import deploy_status
        d = deploy_status(data_dir, candidate)
        return {"built": bool(d.get("checkout_built")), "deployed": bool(d.get("deployed"))}
    except Exception:
        page = Path(data_dir) / "deliverables" / candidate / "checkout.html"
        return {"built": page.is_file(), "deployed": False}


def _pipeline_step_order():
    from .pipeline import STEP_ORDER
    return STEP_ORDER


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------

_STATE_LABEL = {
    "running": "RUNNING", "waiting": "WAITING FOR HUMAN", "human": "WAITING FOR HUMAN",
    "idle": "IDLE", "disabled": "DISABLED", "paused": "PAUSED",
    "planned": "PLANNED", "handled": "HANDLED BY YOU",
    "completed": "COMPLETED", "failed": "FAILED", "blocked": "BLOCKED",
}

# One distinct 24x24 line-icon per roster agent, drawn in the agent's own
# accent colour. Same visual language as the revenue dashboard's _svg().
_AGENT_ICON = {
    "market_scanner": _svg('<circle cx="12" cy="12" r="3"/><path d="M12 3a9 9 0 0 1 9 9"/>'
                           '<path d="M12 7a5 5 0 0 1 5 5"/><path d="M12 12 20 8"/>'),
    "opportunity_finder": _svg('<path d="M4 5h16l-6 8v6l-4-2v-4z"/>'),
    "product_researcher": _svg('<circle cx="10" cy="10" r="6"/><path d="m20 20-5.5-5.5"/>'
                               '<path d="M10 7v6M7 10h6"/>'),
    "trend_hunter": _svg('<path d="M3 17 9 11l4 4 8-8"/><path d="M17 4h4v4"/>'),
    "competitor_analyzer": _svg('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/>'
                                '<path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>'),
    "supplier_finder": _svg('<path d="M12 3 3 7.5V16l9 5 9-5V7.5Z"/>'
                            '<path d="M3 7.5 12 12l9-4.5M12 12v9"/>'),
    "content_creator": _svg('<rect x="5" y="3" width="14" height="18" rx="1.5"/>'
                            '<path d="M9 8h6M9 12h6M9 16h3"/>'),
    "copywriter": _svg('<path d="M4 20h16"/><path d="m14 4 6 6-9 9H5v-6z"/>'),
    "designer": _svg('<path d="M12 3a9 9 0 1 0 0 18c1.4 0 2-1 2-2s-.6-1.2-.6-2 .8-1.6 1.6-1.6H18'
                     'a3 3 0 0 0 3-3c0-4.4-4-6.8-9-6.8Z"/><circle cx="8.5" cy="11" r="1"/>'
                     '<circle cx="12" cy="8" r="1"/><circle cx="15.5" cy="11" r="1"/>'),
    "store_builder": _svg('<path d="M4 9h16v11H4z"/><path d="M3 9 5 4h14l2 5"/>'
                          '<path d="M9 20v-6h6v6"/>'),
    "developer": _svg('<path d="m8 8-4 4 4 4M16 8l4 4-4 4M13.5 6l-3 12"/>'),
    "automation_engineer": _svg('<circle cx="12" cy="12" r="3.4"/>'
                                '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6 7.7 7.7'
                                'M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>'),
    "prospect_scout": _svg('<circle cx="7" cy="14" r="3.4"/><circle cx="17" cy="14" r="3.4"/>'
                           '<path d="m10.4 13 1.6-7 1.6 7M7 10.6 8 5M17 10.6 16 5"/>'),
    "opportunity_scorer": _svg('<path d="M4 15a8 8 0 0 1 16 0"/><path d="M12 15 16 10"/>'
                               '<circle cx="12" cy="15" r="1.4"/>'),
    "outreach_drafter": _svg('<path d="M4 5h16v10H9l-4 4V5Z"/><path d="M8 10h5"/>'),
    "ads_manager": _svg('<path d="M3 11v2l12 5V6L3 11Z"/><path d="M15 9a4 4 0 0 1 0 6"/>'
                        '<path d="M6 13.5V17l3 1"/>'),
    "campaign_optimizer": _svg('<path d="M4 7h16M4 12h16M4 17h16"/><circle cx="9" cy="7" r="2"/>'
                               '<circle cx="15" cy="12" r="2"/><circle cx="8" cy="17" r="2"/>'),
    "budget_allocator": _svg('<circle cx="12" cy="12" r="8"/><path d="M12 12V4M12 12l7 4"/>'),
    "sales_tracker": _svg('<path d="M6 3h12v18l-3-2-3 2-3-2-3 2Z"/><path d="M9 8h6M9 12h6M9 16h3"/>'),
    "profit_master": _svg('<ellipse cx="12" cy="6" rx="7" ry="3"/>'
                          '<path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/>'
                          '<path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>'),
    "revenue_analyst": _svg('<path d="M4 20h16"/><path d="M7 16v-5M12 16V8M17 16v-3"/>'),
    "customer_support": _svg('<path d="M4 13v-1a8 8 0 0 1 16 0v1"/>'
                             '<rect x="3" y="13" width="4" height="6" rx="1.4"/>'
                             '<rect x="17" y="13" width="4" height="6" rx="1.4"/>'
                             '<path d="M20 19a4 4 0 0 1-4 4h-2"/>'),
    "review_manager": _svg('<path d="M4 5h16v11H9l-4 4V5Z"/>'
                           '<path d="m12 7 1.1 2.3 2.5.3-1.8 1.8.4 2.5L12 13l-2.2 1.1.4-2.5'
                           'L8.4 9.6l2.5-.3Z"/>'),
    "quality_control": _svg('<path d="M12 3 5 6v6c0 4 3 6.5 7 8 4-1.5 7-4 7-8V6Z"/>'
                            '<path d="m9 12 2 2 4-4"/>'),
}

_AGENT_ACCENT = {
    "market_scanner": "#38bdf8", "opportunity_finder": "#5eead4",
    "product_researcher": "#34d399", "trend_hunter": "#c084fc",
    "competitor_analyzer": "#fb923c", "supplier_finder": "#22d3ee",
    "content_creator": "#60a5fa", "copywriter": "#facc15", "designer": "#f472b6",
    "store_builder": "#fbbf24", "developer": "#a78bfa",
    "automation_engineer": "#f87171",
    "prospect_scout": "#38bdf8", "opportunity_scorer": "#5eead4",
    "outreach_drafter": "#facc15",
    "ads_manager": "#fb7185", "campaign_optimizer": "#818cf8",
    "budget_allocator": "#fbbf24",
    "sales_tracker": "#4ade80", "profit_master": "#34d399",
    "revenue_analyst": "#4ade80",
    "customer_support": "#38bdf8", "review_manager": "#facc15",
    "quality_control": "#a3e635",
}


def _agent_accent(agent_id: str) -> str:
    return _AGENT_ACCENT.get(agent_id, "#94a3b8")


def _agent_avatar(agent_id: str) -> str:
    icon = _AGENT_ICON.get(agent_id) or _svg(
        '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/>')
    return f"<span class=j-av>{icon}</span>"


def _jarvis_line(snap: dict) -> str:
    """One-line status summary shown on screen. Built only from real state."""
    c = snap["counts"]
    if snap["paused"]:
        return f"Fleet PAUSED. {c['total']} agents held. Nothing runs until you resume."
    job = snap.get("job") or {}
    if job.get("running"):
        p = snap["pipeline"]
        stp = p.get("current_step") or "the fleet"
        return (f"WORKING - {job['what']} at '{stp}'. "
                f"{p['done']} of {p['total']} pipeline steps done.")
    recs = snap.get("recommendations") or []
    top = recs[0] if recs else None
    if top and top["severity"] == "critical":
        return f"CRITICAL: {top['title']}."
    parts = [f"{c['live']} agents online"]
    if c["running"]:
        parts.append(f"{c['running']} running")
    if c["completed"]:
        parts.append(f"{c['completed']} completed")
    if c["waiting_human"] or snap.get("open_gates"):
        parts.append(f"{snap.get('open_gates', c['waiting_human'])} human action(s) for you")
    if c["disabled"]:
        parts.append(f"{c['disabled']} disabled")
    if snap["blockers"]:
        parts.append(f"{len(snap['blockers'])} blocker(s)")
    return ". ".join(parts) + "."


def _voice_line(snap: dict) -> str:
    """What JARVIS says aloud (Web Speech, browser-local). Concise, spoken."""
    c = snap["counts"]
    if snap["paused"]:
        return "Fleet paused. Nothing is executing."
    job = snap.get("job") or {}
    if job.get("running"):
        p = snap["pipeline"]
        return (f"Fleet working. {p['done']} of {p['total']} pipeline steps complete.")
    recs = snap.get("recommendations") or []
    if recs and recs[0]["severity"] == "critical":
        return ("Critical blocker detected. "
                + recs[0]["title"].replace("PayPal", "Pay Pal") + ".")
    gates = snap.get("open_gates", 0)
    if gates:
        return (f"Fleet online. {c['live']} agents available. "
                f"{gates} item{'s' if gates != 1 else ''} require your attention.")
    return f"Fleet online. {c['live']} agents available. Nothing requires you right now."


def _agent_card(a: dict, csrf: str, busy: bool = False) -> str:
    tok = f"<input type=hidden name=csrf value='{_esc(csrf)}'><input type=hidden name=agent value='{_esc(a['id'])}'>"
    toggle_action = "disable" if a["enabled"] else "enable"
    toggle_label = "Disable" if a["enabled"] else "Enable"
    toggle = (
        f"<form method=post action=/control class=j-inline>{tok}"
        f"<button class='j-btn j-{toggle_action}' name=action value={toggle_action}>{toggle_label}</button></form>"
    )
    if busy:
        run = "<button class='j-btn j-run' disabled title=\"a fleet job is running\">Run</button>"
    elif a["runnable_here"] and a["can_run_now"]:
        run = (f"<form method=post action=/control class=j-inline>{tok}"
               f"<button class='j-btn j-run' name=action value=run>Run now</button></form>")
    else:
        why = a["run_blocked_reason"] or "not runnable here"
        run = f"<button class='j-btn j-run' disabled title=\"{_esc(why)}\">Run</button>"

    why_block = (f"<p class=j-why><b>Waiting for human:</b> {_esc(a['why_waiting'])}</p>"
                 if a["why_waiting"] else "")
    lr = f" → {_esc(a['last_result'])}" if a.get("last_result") else ""
    last = (f"<span class=j-last>{_esc(a['last'])}{lr}</span>" if a["last"]
            else "<span class=j-last>no activity recorded</span>")
    deps = a.get("dependencies") or []
    dep_line = ("<div class=j-deps>needs: " + ", ".join(
        f"<span class='{'ok' if d['met'] else 'no'}'>{_esc(d['name'])}</span>"
        for d in deps) + "</div>") if deps else ""
    bar = (
        f"<div class='j-bar k-{a['progress_kind']}' "
        f"role=progressbar aria-valuenow={a['progress']} aria-valuemin=0 aria-valuemax=100>"
        f"<div class=j-bar-fill style='width:{a['progress']}%'></div>"
        f"<span class=j-bar-t>{a['progress']}% · {_esc(a['progress_label'])}</span></div>"
    )

    # --- human-gate resolution ------------------------------------
    gate_block = ""
    if a["human_gated"]:
        hint = (f"<p class=j-hint><b>To handle:</b> {_esc(a['next_step_hint'])}</p>"
                if a["next_step_hint"] else "")
        if a["gate_acknowledged"]:
            note = f" — “{_esc(a['gate_ack_note'])}”" if a["gate_ack_note"] else ""
            gate_block = (
                f"{hint}<p class=j-done>✓ handled by {_esc(a['gate_ack_by'] or 'you')} "
                f"· {_esc(a['gate_ack_at'])}{note}</p>"
                f"<form method=post action=/control class=j-inline>{tok}"
                f"<button class='j-btn' name=action value=reopen-gate>Re-open gate</button></form>"
            )
        else:
            gate_block = (
                f"{hint}"
                f"<form method=post action=/control class='j-inline j-ack'>{tok}"
                f"<input name=note maxlength=120 placeholder='what you did (optional)'>"
                f"<button class='j-btn j-ack-btn' name=action value=ack-gate>✓ Mark handled</button></form>"
            )

    return (
        f"<div class='j-card s-{a['state']}' style=\"--acc:{_agent_accent(a['id'])}\">"
        f"<div class=j-card-h>{_agent_avatar(a['id'])}"
        f"<a class=j-name href='?agent={_esc(a['id'])}'>{_esc(a['name'])}</a>"
        f"<span class=j-pill>{_STATE_LABEL.get(a['state'], a['state'].upper())}</span></div>"
        f"<div class=j-meta>{_esc(a['cluster'])} · {_esc(a.get('description') or a['role'])}"
        f"{' · human-gated' if a['gate'] == 'human' else ''}</div>"
        f"{bar}"
        f"{dep_line}"
        f"{why_block}{last}"
        f"{gate_block}"
        f"<div class=j-actions>{toggle}{run}"
        f"<a class=j-detail href='?agent={_esc(a['id'])}'>details</a>"
        f"<span class=j-runs>runs {a['runs']}</span></div>"
        "</div>"
    )


def _autonomy_panel(auto: dict) -> str:
    st = auto.get("state") or {}
    if not st:
        return ""
    c = auto.get("opportunity_counts") or {}
    blockers = st.get("blockers") or []
    bl = ("".join(f"<li>[{_esc(b.get('class', ''))}] {_esc(b.get('what', ''))} — "
                  f"{_esc(b.get('why', ''))}</li>" for b in blockers))
    bl = f"<ul class=au-bl>{bl}</ul>" if bl else "<p>none — the loop is free to work</p>"
    return (
        "<section class='panel accent'><h3>◆ AUTONOMOUS LOOP</h3>"
        f"<div class=fs-row><span>cycles run</span><span>{st.get('cycles', 0)}"
        f" · last {_esc(_ts_short(st.get('last_cycle_at')))}</span></div>"
        f"<div class=fs-row><span>current objective</span><span>{_esc(st.get('objective', '—'))}</span></div>"
        f"<div class=fs-row><span>current experiment</span><span>{_esc(st.get('current_experiment') or '—')}</span></div>"
        f"<div class=fs-row><span>next action</span><span>{_esc(st.get('next_action', '—'))}</span></div>"
        f"<p class=au-reason>{_esc(st.get('reasoning', ''))}</p>"
        f"<div class=fs-row><span>opportunities</span><span>"
        f"{c.get('discovered', 0)} discovered · {c.get('evaluating', 0)} evaluating · "
        f"{c.get('building', 0)} building · {c.get('testing', 0)} testing · "
        f"{c.get('active', 0)} active · {c.get('successful', 0)} successful · "
        f"{c.get('abandoned', 0)} abandoned</span></div>"
        f"<div><b>blockers:</b> {bl}</div>"
        "</section>"
    )


_OPP_COLS = ("discovered", "evaluating", "building", "testing", "active",
             "successful", "abandoned")


def _opportunity_board_panel(board: dict) -> str:
    if not board:
        return ""
    cols = ""
    for col in _OPP_COLS:
        items = (board.get(col) or [])[:8]
        rows = "".join(
            f"<li title=\"{_esc(r.get('category', ''))} · score {r.get('score', 0)}\">"
            f"{_esc(r.get('title', ''))[:60]}"
            + (f" <b>EUR {r['results'].get('revenue_eur', 0):.0f}</b>"
               if (r.get('results') or {}).get('revenue_eur') else "")
            + "</li>"
            for r in items)
        more = (len(board.get(col) or []) - len(items))
        more = f"<li class=more>+{more} more</li>" if more > 0 else ""
        cols += (f"<div class='opp-col oc-{col}'><div class=opp-h>{col.upper()} "
                 f"({len(board.get(col) or [])})</div><ul>{rows}{more}</ul></div>")
    return (f"<section class=panel><h3>OPPORTUNITY BOARD — the fleet is not "
            f"locked to one business model</h3><div class=opp-board>{cols}</div>"
            f"</section>")


def _execution_panel(execution: list, board: dict, csrf: str) -> str:
    """Opportunity ACCEPTANCE - a business decision, separate from the human
    approval panel. Accepting builds a real ExecutionTask chain."""
    execution = execution or []
    accepted_ids = {row["opportunity_id"] for row in execution}

    body = ""
    for row in execution:
        oid = row["opportunity_id"]
        chips = "".join(
            f"<span class='xtask x-{t['status'].lower()}' "
            f"title=\"{_esc(t['task_type'])}: {_esc(t['status'])}"
            + (f" — {_esc(t['error'])}" if t.get("error") else "") + "\">"
            f"{_esc(t['task_type'].replace('_', ' ').lower())}</span>"
            for t in row.get("tasks", []))
        nxt = row.get("current_task") or row.get("next_task") or "—"
        blk = (f"<div class=x-blk>blocked: {_esc(row['blocker'])} — approve it "
               f"in HUMAN APPROVALS</div>" if row.get("blocker") else "")
        abandon = _cbtn(csrf, "abandon-opportunity", "Abandon", "j-btn j-disable",
                        hidden={"opp": oid})
        body += (
            f"<div class=xrow>"
            f"<div class=xrow-h><b>{_esc(row.get('title', ''))[:70]}</b>"
            f"<span class=xstate>{_esc(row.get('state', ''))}</span></div>"
            f"<div class=xchips>{chips}</div>"
            f"<div class=xmeta>next: <b>{_esc(nxt)}</b>"
            f" · accepted by {_esc(row.get('accepted_by') or 'you')}</div>"
            f"{blk}"
            f"<div class=j-actions>{abandon}</div>"
            f"</div>")

    # acceptable = discovered / evaluating opportunities not already accepted
    acceptable = []
    for col in ("evaluating", "discovered"):
        for r in (board.get(col) or []):
            if r["id"] in accepted_ids:
                continue
            acceptable.append(r)
    acc_html = ""
    for r in acceptable[:6]:
        btn = _cbtn(csrf, "accept-opportunity", "Accept →", "j-btn j-enable",
                    hidden={"opp": r["id"]})
        acc_html += (f"<div class=xcand><span>{_esc(r.get('title', ''))[:64]}"
                     f"<small> · {_esc(r.get('category', ''))} · score "
                     f"{r.get('score', 0)}</small></span>{btn}</div>")
    if not acc_html:
        acc_html = "<p>no un-accepted opportunities on the shortlist right now</p>"

    run_btn = _cbtn(csrf, "run-worker", "Run worker", "j-btn",
                    hidden={"max_ticks": "50"})
    if not body:
        body = ("<p>Nothing accepted yet. Accepting an opportunity is a "
                "business decision — it does not spend money or perform any "
                "protected action; the money / identity / legal gates still "
                "sit inside the task chain.</p>")
    return (
        f"<section class='panel accent'><h3>EXECUTION — accepted opportunities "
        f"&amp; their task chains</h3>"
        f"<div class=j-actions>{run_btn}<span class=x-note>drains the queue "
        f"once; AUTONOMOUS mode drains it every cycle</span></div>"
        f"{body}"
        f"<h4 class=x-h>ACCEPT AN OPPORTUNITY</h4>{acc_html}"
        f"</section>")


_APPR_ICON = {"money": "💰", "identity": "🪪", "legal": "⚖️"}


def _approvals_panel(pending: dict, csrf: str) -> str:
    total = sum(len(pending.get(k) or []) for k in ("money", "identity", "legal"))
    head = (f"<h3>HUMAN APPROVALS — the ONLY things that need you "
            f"({total} pending)</h3>")
    if total == 0:
        return (f"<section class=panel>{head}<p>Nothing needs you. The fleet is "
                f"working autonomously on everything else.</p></section>")
    body = ""
    for kind in ("money", "identity", "legal"):
        reqs = pending.get(kind) or []
        if not reqs:
            continue
        body += f"<div class=appr-k>{_APPR_ICON[kind]} {kind.upper()}</div>"
        for r in reqs:
            tok = _tok(csrf, id=r.get("id", ""))
            extra = ""
            if kind == "money":
                extra = (f"<div class=appr-x><b>amount:</b> {r.get('amount', 0)} "
                         f"{r.get('currency', 'EUR')} · <b>necessity:</b> "
                         f"{_esc(r.get('necessity', 'optional'))} · <b>max budget:</b> "
                         f"{r.get('recommended_max_budget', '—')}</div>"
                         f"<div class=appr-x><b>benefit:</b> {_esc(r.get('expected_benefit', ''))} · "
                         f"<b>downside:</b> {_esc(r.get('downside', 'none'))} · "
                         f"<b>ROI:</b> {_esc(r.get('expected_roi', 'unknown'))}</div>")
            else:
                extra = f"<div class=appr-x><b>boundary:</b> {_esc(r.get('boundary', ''))}</div>"
            body += (
                f"<div class=appr>"
                f"<div class=appr-what>{_esc(r.get('what', ''))}</div>"
                f"<div class=appr-why><b>why:</b> {_esc(r.get('why', ''))}</div>"
                f"{extra}"
                f"<div class=appr-after><b>after you approve:</b> "
                f"{_esc(r.get('what_happens_after', ''))}</div>"
                f"<div class=j-actions>"
                f"<form method=post action=/control class=j-inline>{tok}"
                f"<button class='j-btn j-enable' name=action value=approve-request>Approve</button></form>"
                f"<form method=post action=/control class=j-inline>{tok}"
                f"<button class='j-btn j-disable' name=action value=deny-request>Deny</button></form>"
                f"</div></div>"
            )
    return f"<section class='panel accent'>{head}{body}</section>"


_STEP_GLYPH = {"ok": "✓", "skipped": "○", "running": "▶", "blocked": "⏸",
               "failed": "✗", "stopped": "■", "pending": "·"}
_STEP_WORD = {"ok": "COMPLETE", "skipped": "SKIPPED", "running": "RUNNING",
              "blocked": "WAITING FOR HUMAN", "failed": "FAILED",
              "stopped": "STOPPED", "pending": "PENDING"}


def _pipeline_panel(pipe: dict, job: dict) -> str:
    running = bool(job.get("running"))
    tag = " <span class=run>running…</span>" if running else ""
    kind = "bad" if pipe.get("bad") else ("run" if running else "ok")
    head = (f"PIPELINE — <b>{_esc(pipe.get('candidate') or 'none')}</b> · "
            f"{_esc((pipe.get('status') or 'idle').upper())} · "
            f"{pipe.get('done', 0)}/{pipe.get('total', 0)} steps{tag}")
    bar = (f"<div class='j-bar k-{kind}' role=progressbar aria-valuenow={pipe.get('pct', 0)}>"
           f"<div class=j-bar-fill style='width:{pipe.get('pct', 0)}%'></div>"
           f"<span class=j-bar-t>{pipe.get('pct', 0)}%</span></div>")
    rows = ""
    for s in pipe.get("steps", []):
        st = s.get("status", "pending")
        reason = s.get("reason") or ""
        detail = (f" — {_esc(reason)}" if reason else "")
        rows += (
            f"<div class='pstep p-{st}'>"
            f"<span class=pg>{_STEP_GLYPH.get(st, '·')}</span>"
            f"<span class=pcap>{_esc(s['cap'])}</span>"
            f"<span class=pagent>{_esc(s.get('agent', ''))}</span>"
            f"<span class=pstat>{_STEP_WORD.get(st, st.upper())}{detail}</span>"
            "</div>"
        )
    gate = pipe.get("human_gate") or {}
    grsn = (f"<p class=j-why>{_esc(gate.get('reason', ''))}</p>"
            if gate.get("reason") else "")
    err = (f"<p class=j-why>{_esc(pipe.get('error'))}</p>" if pipe.get("error") else "")
    return (f"<section class=panel><div class=j-pipe-h>{head}</div>{bar}"
            f"<div class=psteps>{rows}</div>{err}{grsn}</section>")


def _recommends_panel(recs: list) -> str:
    if not recs:
        return ""
    rows = ""
    for r in recs:
        rows += (f"<div class='rec sev-{r['severity']}'>"
                 f"<div class=rec-h><span class=rec-sev>{r['severity'].upper()}</span>"
                 f"<b>{_esc(r['title'])}</b></div>"
                 f"<p>{_esc(r['detail'])}</p></div>")
    return (f"<section class='panel accent'><h3>◆ JARVIS RECOMMENDS</h3>{rows}"
            f"<p class=note>Deterministic — derived only from repository state, no LLM.</p>"
            f"</section>")


def _human_actions_panel(items: list, csrf: str) -> str:
    if not items:
        return ("<section class=panel><h3>HUMAN ACTIONS</h3>"
                "<p>Nothing is waiting on you.</p></section>")
    rows = ""
    for it in items:
        tags = []
        if it.get("affects_money"):
            tags.append("<span class='tag money'>affects money</span>")
        if it.get("affects_external"):
            tags.append("<span class='tag ext'>external</span>")
        if it.get("jarvis_can_prepare"):
            tags.append("<span class='tag prep'>JARVIS can prepare</span>")
        rows += (
            f"<div class=ha>"
            f"<div class=ha-h><span class=ha-area>{_esc(it['area'])}</span>"
            f"<span class='tag st'>{_esc(it['status'])}</span>{''.join(tags)}</div>"
            f"<div class=ha-what>{_esc(it['what'])}</div>"
            f"<div class=ha-do><b>Do:</b> {_esc(it['human_action'])}</div>"
            "</div>"
        )
    return f"<section class=panel><h3>HUMAN ACTIONS — exactly what you need to do</h3>{rows}</section>"


def _financial_panel(fin: dict) -> str:
    an = fin.get("anthropic", {})
    pp = fin.get("paypal", {})
    def _st(v, good="READY", ok=("READY", "AUTHORIZED")):
        cls = "ok" if v in ok else ("bad" if v in ("BLOCKED",) else "warn")
        return f"<span class='fs {cls}'>{_esc(v)}</span>"
    rows = [
        ("Anthropic / LLM", f"{_st(an.get('state', 'DISABLED'), ok=('AUTHORIZED',))} · "
         f"{an.get('api_calls', 0)} calls · ${an.get('spent_usd', 0):.2f}"),
        ("PayPal", _st(pp.get("state", "?"))),
        ("External spend", f"${fin.get('external_spend_usd', 0):.2f}"),
        ("Pre-sale limit", f"EUR {fin.get('presale_limit_eur', '?')} "
         f"(${fin.get('presale_limit_usd', '?')}) · "
         f"${fin.get('presale_remaining_usd', '?')} remaining"),
        ("Revenue", f"EUR {fin.get('revenue_eur', 0):.2f}"),
        ("Money actions", "<span class='fs warn'>HUMAN ONLY</span>"),
        ("Can JARVIS spend now?", "<span class='fs ok'>NO</span>"),
    ]
    body = "".join(f"<div class=fs-row><span>{k}</span><span>{v}</span></div>"
                   for k, v in rows)
    return f"<section class='panel'><h3>◆ FINANCIAL SAFETY</h3>{body}</section>"


def _llm_budget_panel(llm: dict) -> str:
    if not llm:
        return ""
    p = llm.get("policy", {})
    b = llm.get("balances", {})
    ok = llm.get("available")
    state = ("EMERGENCY STOP" if p.get("emergency_stop")
             else "AVAILABLE" if ok
             else "DISABLED")
    cls = "bad" if p.get("emergency_stop") else ("ok" if ok else "warn")
    rows = [
        ("LLM tier", f"<span class='fs {cls}'>{state}</span> "
         f"· provider {_esc(p.get('provider', 'none'))} · model {_esc(p.get('model', '—'))}"),
        ("Autonomous loop may use it?",
         "<span class='fs ok'>YES</span>" if p.get("autonomous_enabled")
         else "<span class='fs warn'>NO</span>"),
        ("Per-call / hourly / daily / global",
         f"${p.get('per_call_usd', 0)} / ${p.get('hourly_usd', 0)} / "
         f"${p.get('daily_usd', 0)} / ${p.get('global_usd', 0)}"),
        ("Rate limit", f"{p.get('max_calls_per_min', 0)}/min "
         f"({b.get('calls_last_minute', 0)} used this minute)"),
        ("Spent hour / today / total",
         f"${b.get('spent_this_hour', 0):.4f} / ${b.get('spent_today', 0):.4f} / "
         f"${b.get('spent_total', 0):.4f}"),
        ("Remaining hour / today / global",
         f"${b.get('remaining_hour', 0):.2f} / ${b.get('remaining_today', 0):.2f} / "
         f"${b.get('remaining_global', 0):.2f}"),
    ]
    if not ok and llm.get("reason"):
        rows.append(("Why unavailable", _esc(llm["reason"])))
    body = "".join(f"<div class=fs-row><span>{k}</span><span>{v}</span></div>"
                   for k, v in rows)
    calls = llm.get("recent_calls") or []
    feed = ""
    if calls:
        feed = "<div class=feed style='max-height:140px'>" + "".join(
            f"<div class=ev><span class=ev-t>{_esc(_ts_short(c.get('ts')))}</span>"
            f"<span class=ev-k>{_esc(c.get('task', ''))}</span>"
            f"<span class=ev-x>${c.get('actual_usd', 0):.4f} · {_esc(c.get('outcome', ''))}"
            f"{' · autonomous' if c.get('autonomous') else ''}</span></div>"
            for c in calls) + "</div>"
    return (f"<section class='panel'><h3>◆ LLM BUDGET & AUDIT — "
            f"configure limits before connecting credits</h3>{body}{feed}"
            f"<p class=note>CLI: <b>revenue_os llm-policy</b> (--enable, "
            f"--enable-autonomous, --emergency-stop, --per-call-usd, ...). "
            f"With the LLM off, every agent runs its deterministic path.</p>"
            f"</section>")


def _profit_bar(label: str, w: dict, scale_max: float) -> str:
    profit = w.get("profit_eur", 0.0)
    pct = min(100.0, round(50 * abs(profit) / scale_max)) if scale_max else 0
    side = "pos" if profit >= 0 else "neg"
    return (
        "<div class=ps-row>"
        f"<span class=ps-label>{_esc(label)}</span>"
        "<div class=ps-track><span class=ps-mid></span>"
        f"<span class='ps-fill {side}' style='width:{pct}%'></span></div>"
        f"<span class='ps-val {side}'>{'+' if profit >= 0 else ''}EUR {profit:.2f}</span>"
        f"<span class=ps-sub>rev EUR {w.get('revenue_eur', 0):.2f} · "
        f"AI spend ${w.get('spend_usd', 0):.2f}</span>"
        "</div>"
    )


def _profit_scale_panel(ps: dict) -> str:
    if not ps:
        return ""
    daily, weekly = ps.get("daily") or {}, ps.get("weekly") or {}
    scale_max = float(ps.get("scale_max_eur", 1.0) or 1.0)
    body = _profit_bar("DAILY (24h)", daily, scale_max) + _profit_bar("WEEKLY (7d)", weekly, scale_max)
    return (f"<section class=panel><h3>◆ PROFIT SCALE — booked revenue minus AI spend, "
            f"real ledger entries only</h3>{body}</section>")


_STAGE_DOT = {"green": "🟢", "amber": "🟡", "red": "🔴", "off": "⚪"}


def _revenue_pipeline_panel(stages: list) -> str:
    if not stages:
        return ""
    rows = ""
    for s in stages:
        rows += (f"<div class='rp rp-{s['state']}'>"
                 f"<span class=rp-dot>{_STAGE_DOT.get(s['state'], '⚪')}</span>"
                 f"<span class=rp-stage>{_esc(s['stage'])}</span>"
                 f"<span class=rp-note>{_esc(s['note'])}</span></div>")
    return (f"<section class=panel><h3>CUSTOMER → REVENUE PIPELINE</h3>{rows}</section>")


def _acquisition_panel(acq: dict, csrf: str) -> str:
    ld = acq.get("last_discovery") or {}
    when = _ts_short(ld.get("ts")) if isinstance(ld, dict) else ""
    summary = (f"<div class=acq-sum>"
               f"<span><b>{acq.get('total', 0)}</b> leads</span>"
               f"<span><b>{acq.get('fresh', 0)}</b> fresh</span>"
               f"<span><b>{acq.get('stale', 0)}</b> stale</span>"
               f"<span><b>{acq.get('high_quality', 0)}</b> high-quality</span>"
               f"<span><b>{acq.get('awaiting_outreach', 0)}</b> awaiting outreach</span>"
               f"<span><b>{acq.get('outreach_drafts', 0)}</b> drafts</span>"
               f"<span>last discovery {when or 'never'}</span></div>")
    rows = ""
    for l in (acq.get("leads") or [])[:12]:
        prep = ""
        if not l.get("has_draft") and "Skip" not in l["recommended_action"]:
            prep = (f"<form method=post action=/control class=j-inline>"
                    f"<input type=hidden name=csrf value='{_esc(csrf)}'>"
                    f"<input type=hidden name=action value=prepare-outreach>"
                    f"<input type=hidden name=lead_id value='{_esc(l['lead_id'])}'>"
                    f"<button class='j-btn j-run'>Prepare Outreach</button></form>")
        rows += (
            f"<div class=lead>"
            f"<span class=lead-score>{l.get('score', 0)}</span>"
            f"<span class=lead-sig>{_esc(l.get('signal', ''))}</span>"
            f"<span class=lead-meta>{_esc(str(l.get('age_days')))}d · {_esc(l.get('source', ''))} · {_esc(l.get('status', ''))}</span>"
            f"<span class=lead-rec>{_esc(l.get('recommended_action', ''))}</span>"
            f"{prep}</div>"
        )
    if not rows:
        rows = "<p>No leads. Run discovery from the CLI (`revenue_os discover-free`).</p>"
    return (f"<section class=panel><h3>ACQUISITION — JARVIS never contacts a lead</h3>"
            f"{summary}{rows}</section>")


def _activity_feed_panel(events: list) -> str:
    if not events:
        return ("<section class=panel><h3>ACTIVITY</h3>"
                "<p>No events recorded yet.</p></section>")
    rows = "".join(
        f"<div class=ev><span class=ev-t>{_esc(_ts_short(e.get('ts')))}</span>"
        f"<span class=ev-k>{_esc(e.get('kind', ''))}</span>"
        f"<span class=ev-x>{_esc(e.get('text', ''))}</span></div>"
        for e in events
    )
    return (f"<section class=panel><h3>ACTIVITY — only what actually happened</h3>"
            f"<div class=feed>{rows}</div></section>")


# ---------------------------------------------------------------------------
# the ECOSYSTEM - 24 cute agent creatures that actually work together
# ---------------------------------------------------------------------------

# cluster zones on a 1120 x 520 canvas
_ECO_ZONES = {
    "discovery":   (200, 140), "build":       (560, 140), "acquisition": (920, 140),
    "marketing":   (200, 380), "revenue":     (560, 380), "support":     (920, 380),
}
# offsets within a zone (up to 6 agents), a friendly little cluster
_ECO_SLOTS = [(-90, -46), (0, -66), (90, -46), (-90, 46), (0, 66), (90, 46)]


def _ecosystem_data(snap: dict) -> dict:
    """Nodes + edges for the ecosystem, all from real state."""
    agents = {a["id"]: a for a in snap.get("agents", [])}
    auto = snap.get("autonomy") or {}
    flows = (auto.get("state") or {}).get("recent_flows", []) or []
    job = snap.get("job") or {}
    job_running = bool(job.get("running"))
    autonomy_working = job_running and "autonomy" in str(job.get("what", ""))
    pipe_steps = {s["cap"]: s.get("status")
                  for s in (snap.get("pipeline") or {}).get("steps", [])}
    # the agents the autonomous build chain cycles through every ~45s
    _CHAIN_IDS = {"opportunity_finder", "content_creator", "designer",
                  "developer", "quality_control"}

    by_cluster: dict[str, list] = {c: [] for c in roster.CLUSTERS}
    for spec in roster.AGENTS:
        by_cluster.setdefault(spec.cluster, []).append(spec)

    nodes = {}
    for cluster, specs in by_cluster.items():
        cx, cy = _ECO_ZONES.get(cluster, (560, 260))
        for i, spec in enumerate(specs[:6]):
            ox, oy = _ECO_SLOTS[i] if i < len(_ECO_SLOTS) else (0, 0)
            a = agents.get(spec.id, {})
            st = a.get("state", "idle")
            # is this creature busy right now?
            busy = (st == "running"
                    or pipe_steps.get(spec.capability) == "running"
                    or (autonomy_working and spec.id in _CHAIN_IDS)
                    or (job_running and any(f["from"] == spec.id or f["to"] == spec.id
                                            for f in flows[-8:])))
            mood = ("work" if busy else "sleep" if st in ("disabled", "paused")
                    else "wait" if st in ("waiting", "human") else "happy")
            nodes[spec.id] = {
                "id": spec.id, "name": spec.name, "cluster": cluster,
                "x": cx + ox, "y": cy + oy,
                "accent": _agent_accent(spec.id),
                "state": st, "busy": busy, "mood": mood,
                "progress": a.get("progress", 0),
            }

    edges = []
    seen = set()
    # 1) the wiring: who depends on whose output (faint, always there)
    for spec in roster.AGENTS:
        for dep in spec.depends_on:
            if dep in nodes and spec.id in nodes and (dep, spec.id) not in seen:
                seen.add((dep, spec.id))
                edges.append({"from": dep, "to": spec.id, "kind": "wire"})
    # 2) real recent data flows from the autonomy build chain (bright, animated)
    recent = flows[-14:]
    for i, f in enumerate(recent):
        if f["from"] in nodes and f["to"] in nodes:
            live = job_running and i >= len(recent) - 6
            edges.append({"from": f["from"], "to": f["to"],
                          "kind": "live" if live else "flow"})
    return {"nodes": list(nodes.values()), "edges": edges,
            "working": sum(1 for n in nodes.values() if n["busy"]),
            "flows": len([e for e in edges if e["kind"] in ("flow", "live")])}


def _eco_creature(n: dict) -> str:
    """One round, colourful agent creature: a big centred avatar glyph with
    a small face. `data-hx/hy` are its home position; the ambient-wander
    loop in _JS offsets it from there every frame (no-JS = sits at home)."""
    x, y, acc = n["x"], n["y"], n["accent"]
    icon = _AGENT_ICON.get(n["id"]) or _svg('<circle cx=12 cy=12 r=8/>')
    return (
        f"<g class='eco {'m-' + n['mood']}' data-id='{_esc(n['id'])}' "
        f"data-hx='{x}' data-hy='{y}' transform='translate({x},{y})' "
        f"style=\"--acc:{acc}\">"
        f"<ellipse class=eco-shadow cx=0 cy=26 rx=18 ry=5/>"
        f"<circle class=eco-ring r=24/>"
        f"<circle class=eco-body r=20/>"
        # tiny eyes peeking over the top of the glyph
        f"<circle cx=-6 cy=-11 r=2 fill=#fff/><circle cx=6 cy=-11 r=2 fill=#fff/>"
        f"<circle class=eco-pupil cx=-6 cy=-11 r=1/><circle class=eco-pupil cx=6 cy=-11 r=1/>"
        # the avatar glyph - now the dominant feature, centred
        f"<g class=eco-icon transform='translate(-11,-10) scale(0.92)'>{icon}</g>"
        f"<path class=eco-smile d='M-4 13 Q0 16 4 13' fill=none stroke=#fff "
        f"stroke-width=1.6 stroke-linecap=round/>"
        f"<text class=eco-name y=42>{_esc(n['name'])}</text>"
        f"</g>"
    )


def _ecosystem_panel(eco: dict) -> str:
    if not eco.get("nodes"):
        return ""
    pos = {n["id"]: (n["x"], n["y"]) for n in eco["nodes"]}
    edge_svg = ""
    for e in eco["edges"]:
        if e["from"] not in pos or e["to"] not in pos:
            continue
        x1, y1 = pos[e["from"]]
        x2, y2 = pos[e["to"]]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2 - 26
        edge_svg += (f"<path class='eco-edge e-{e['kind']}' "
                     f"data-from='{_esc(e['from'])}' data-to='{_esc(e['to'])}' "
                     f"d='M{x1} {y1} Q{mx} {my} {x2} {y2}'/>")
    zones = "".join(
        f"<g class=eco-zone><ellipse cx={cx} cy={cy} rx=150 ry=110/>"
        f"<text x={cx} y={cy - 96}>{c.upper()}</text></g>"
        for c, (cx, cy) in _ECO_ZONES.items())
    creatures = "".join(_eco_creature(n) for n in eco["nodes"])
    return (
        f"<section class=panel><h3>THE ECOSYSTEM — {len(eco['nodes'])} agents, "
        f"{eco['working']} working now, {eco['flows']} live data-flow(s) "
        f"<span id=eco-dbg style=\"color:#5f7c92;font-size:10px;letter-spacing:0\">"
        f"</span></h3>"
        f"<div class=eco-wrap><svg class=eco-svg viewBox='0 0 1120 520' "
        f"preserveAspectRatio='xMidYMid meet'>"
        f"{zones}{edge_svg}{creatures}</svg></div>"
        f"<p class=note>Each creature wears its agent's avatar; the gentle "
        f"drifting is ambient only. What's real: a pulsing ring = that agent "
        f"is running now, greyed-out = disabled/paused, faint links = who "
        f"feeds whom, glowing dashed links = data actually moving this cycle "
        f"(Content Creator → Designer → Developer → Quality Control).</p></section>"
    )


def _vitals(snap: dict) -> str:
    b = snap["budget"]
    rem = b.get("presale_remaining_usd", 0.0) or 0.0
    cap = b.get("presale_cap_usd", 0.0) or 0.0
    pct = int(round(100 * rem / cap)) if cap else 0
    spent = b.get("external_spent_usd", 0.0) or 0.0
    c = snap["counts"]
    p = snap["pipeline"]
    step = p.get("current_step") or ("— none pending" if p.get("status") == "prepared"
                                     else "—")

    chips = [
        ("agents", c["total"]), ("running", c["running"]), ("idle", c["idle"]),
        ("completed", c["completed"]), ("waiting for human", c["waiting_human"]),
        ("blocked", c["blocked"]), ("disabled", c["disabled"]),
    ]
    chip_html = "".join(
        f"<span class='chip{' hot' if (k in ('waiting for human','blocked') and v) else ''}'>"
        f"{v} <small>{k}</small></span>" for k, v in chips)

    tiles = [
        ("PIPELINE STEP", _esc(str(step)),
         f"<small>{_esc((p.get('status') or 'idle'))} · {p.get('done',0)}/{p.get('total',0)}</small>"),
        ("REVENUE", f"€{snap['revenue_eur']:.2f}",
         "<small>first sale unlocks growth capital</small>"),
        ("PRE-SALE BUDGET", f"${rem:.2f}",
         f"<div class=gauge><div class=gauge-fill style='width:{pct}%'></div></div>"
         f"<small>${spent:.2f} spent of ${cap:.2f}</small>"),
        ("OPEN BLOCKERS", str(len(snap["blockers"])),
         "<small>" + (_esc(snap["blockers"][0]["title"]) if snap["blockers"]
                      else "none open") + "</small>"),
    ]
    tile_html = "".join(
        f"<div class=j-tile><div class=j-tile-k>{k}</div>"
        f"<div class=j-tile-v>{v}</div>{extra}</div>" for k, v, extra in tiles)
    return f"<div class=j-chips>{chip_html}</div><div id=vitals class=j-vitals>{tile_html}</div>"


def _blockers_panel(blockers: list, csrf: str) -> str:
    if not blockers:
        return ""
    rows = "".join(
        f"<li><b>{_esc(b.get('title', ''))}</b> "
        f"<span class=mono>[{_esc(b.get('area', ''))}/{_esc(b.get('severity', ''))}]</span> "
        f"<form method=post action=/control class=j-inline>"
        f"<input type=hidden name=csrf value='{_esc(csrf)}'>"
        f"<input type=hidden name=id value='{_esc(b.get('id', ''))}'>"
        f"<button class='j-btn' name=action value=resolve-blocker>Resolve</button></form>"
        f"<br><small>{_esc(b.get('detail', ''))}</small></li>"
        for b in blockers
    )
    return (f"<div class=j-blockers><h3>Open blockers — resolve once you've cleared them"
            f"</h3><ul>{rows}</ul></div>")


def _outreach_panel(briefs: list, csrf: str) -> str:
    if not briefs:
        return ""
    rows = ""
    for b in briefs:
        lid = _esc(b["lead_id"])
        rows += (
            f"<form method=post action=/control class=gate-form>"
            f"<input type=hidden name=csrf value='{_esc(csrf)}'>"
            f"<input type=hidden name=action value=outreach-status>"
            f"<input type=hidden name=lead_id value='{lid}'>"
            f"<span class=who>{_esc(b['title'] or lid)}"
            f"<small> · {_esc(b['platform'])} · {_esc(b['status'])}</small></span>"
            f"<input name=reason type=text placeholder='note (optional)'>"
            f"<button class='gate-btn validated' name=status value=posted>I posted it</button>"
            f"<button class='gate-btn rejected' name=status value=skipped>Skipped</button>"
            f"</form>"
        )
    return (f"<div class=j-gates><h3>Outreach drafts — the system posts nothing; "
            f"log what YOU did</h3>{rows}</div>")


def _gates_panel(queue: list, csrf: str) -> str:
    if not queue:
        return "<div class=j-gates><h3>Candidate gates</h3><p>Nothing waiting.</p></div>"
    forms = "".join(_gate_form(i, csrf) for i in queue)
    return (f"<div class=j-gates><h3>Candidate gates — you decide, agents never do</h3>"
            f"{forms}</div>")


_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#05080d;color:#c8d6e5;font:14px/1.5 'JetBrains Mono',ui-monospace,Menlo,Consolas,monospace}
a{color:#38e8ff}
.wrap{max-width:1180px;margin:0 auto;padding:22px}
h1,h2,h3{font-weight:600;letter-spacing:.04em}
.j-top{display:flex;align-items:center;gap:18px;border-bottom:1px solid #16324a;padding-bottom:16px;margin-bottom:18px}
.reactor{width:52px;height:52px;border-radius:50%;flex:0 0 auto;
 background:radial-gradient(circle at 50% 50%,#8ff9ff 0%,#22c7e6 30%,#0a2b3d 70%);
 box-shadow:0 0 22px #22d3ee88,inset 0 0 10px #063040;animation:pulse 3.4s ease-in-out infinite}
.reactor.off{background:radial-gradient(circle at 50% 50%,#5b6b74,#101820 70%);box-shadow:none;animation:none}
@keyframes pulse{0%,100%{box-shadow:0 0 16px #22d3ee66,inset 0 0 10px #063040}50%{box-shadow:0 0 30px #22d3eecc,inset 0 0 12px #063040}}
.j-title b{font-size:20px;letter-spacing:.22em;color:#eaf6ff}
.j-title small{display:block;color:#5f7c92;letter-spacing:.12em}
.j-sys{margin-left:auto;text-align:right}
.j-sys .big{font-size:18px;letter-spacing:.14em}
.online{color:#4ade80}.holding{color:#fbbf24}
#says{margin:0 0 18px;padding:10px 14px;border-left:3px solid #22d3ee;background:#0a1622;color:#9fd7e6;font-style:italic}
.j-vitals{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.j-tile{background:#0a1420;border:1px solid #17324a;border-radius:8px;padding:12px}
.j-tile-k{font-size:10px;letter-spacing:.16em;color:#5f7c92}
.j-tile-v{font-size:22px;color:#eaf6ff;margin:2px 0 6px}
.j-tile small{color:#6b8aa0}
.gauge{height:6px;background:#12283a;border-radius:3px;overflow:hidden;margin-bottom:4px}
.gauge-fill{height:100%;background:linear-gradient(90deg,#22d3ee,#4ade80)}
.j-pipe{background:#0a1420;border:1px solid #17324a;border-radius:8px;padding:12px;margin-bottom:18px}
.j-pipe-h{font-size:12px;color:#8fb3c8;margin-bottom:8px}
.j-pipe-row{display:flex;flex-wrap:wrap;gap:6px}
.pz{font-size:11px;padding:3px 7px;border-radius:4px;background:#12283a;color:#7f9bb0}
.pz-ok{background:#0f3d2a;color:#6ee7b7}.pz-blocked{background:#4a1220;color:#fca5a5}
.pz-failed{background:#4a1220;color:#fca5a5}.pz-skipped{background:#20303f;color:#9db4c4}
.pz-running{background:#0b3a54;color:#7dd3fc}
.j-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px;align-items:start}
.j-cluster{grid-column:1/-1;margin:14px 0 2px;font-size:11px;letter-spacing:.2em;color:#5f7c92}
.j-card{background:#0a1420;border:1px solid #17324a;border-left:3px solid #2b4a63;border-radius:8px;padding:12px;align-self:start}
.j-card.s-running{border-left-color:#38bdf8}.j-card.s-waiting{border-left-color:#fbbf24}
.j-card.s-human{border-left-color:#a78bfa}.j-card.s-disabled{border-left-color:#4b5563;opacity:.62}
.j-card.s-paused{border-left-color:#4b5563;opacity:.5}.j-card.s-idle{border-left-color:#2b6}
.j-card-h{display:flex;align-items:center;gap:9px}
.j-name{color:#eaf6ff;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.j-av{flex:0 0 auto;width:30px;height:30px;display:flex;align-items:center;justify-content:center;
 border-radius:8px;color:var(--acc);background:color-mix(in srgb,var(--acc) 15%,#0a1420);
 border:1px solid color-mix(in srgb,var(--acc) 40%,#17324a)}
.j-av svg{width:18px;height:18px}
.s-disabled .j-av,.s-paused .j-av{color:#5f7c92;background:#0d1826;border-color:#24384a;filter:grayscale(1)}
.j-pill{font-size:9px;letter-spacing:.12em;padding:2px 6px;border-radius:3px;background:#12283a;color:#8fb3c8;white-space:nowrap}
.j-meta{font-size:11px;color:#5f7c92;margin:3px 0 6px}
.mono{font-family:inherit;color:#7fb0c7}
.j-bar{position:relative;height:16px;border-radius:4px;background:#0d1c2b;border:1px solid #16324a;overflow:hidden;margin:2px 0 7px}
.j-bar-fill{height:100%;width:0;transition:width .6s ease;background:#2b4a63}
.j-bar-t{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
 font-size:9.5px;letter-spacing:.04em;color:#cfe6f0;text-shadow:0 1px 2px #000a}
.j-bar.k-ok .j-bar-fill{background:linear-gradient(90deg,#0f766e,#4ade80)}
.j-bar.k-run .j-bar-fill{background:linear-gradient(90deg,#0b3a54,#38bdf8);
 background-size:28px 28px;background-image:linear-gradient(135deg,#38bdf8 25%,#0b3a54 0,#0b3a54 50%,#38bdf8 0,#38bdf8 75%,#0b3a54 0);
 animation:stripe 1s linear infinite}
.j-bar.k-bad .j-bar-fill{background:linear-gradient(90deg,#7f1d1d,#f87171)}
.j-bar.k-idle .j-bar-fill{background:#24384a}
@keyframes stripe{to{background-position:28px 0}}
.j-pipe-h .run{color:#7dd3fc}
.reactor.busy{animation:pulse 1.1s ease-in-out infinite}
.j-why{margin:6px 0;font-size:12px;color:#ffd48a}
.j-hint{margin:5px 0;font-size:11px;color:#8fb3c8}
.j-done{margin:5px 0;font-size:11px;color:#8ff0c0}
.j-ack{display:flex;gap:5px;margin:6px 0 2px;flex-wrap:wrap}
.j-ack input{flex:1;min-width:120px;background:#05080d;border:1px solid #2b4a63;color:#c8d6e5;padding:3px 6px;border-radius:4px;font:inherit;font-size:11px}
.j-ack-btn{border-color:#2b6b4a;color:#8ff0c0}
.j-card.s-handled{border-left-color:#4ade80;opacity:.85}
.j-card.s-handled .j-av{filter:none}
.j-last{font-size:11px;color:#6b8aa0}
.j-actions{display:flex;align-items:center;gap:6px;margin-top:9px}
.j-inline{display:inline}
.j-btn{font:inherit;font-size:11px;padding:4px 10px;border-radius:4px;border:1px solid #2b4a63;background:#12283a;color:#c8d6e5;cursor:pointer}
.j-btn:hover{border-color:#38e8ff}
.j-btn:disabled{opacity:.4;cursor:not-allowed}
.j-run{border-color:#2b6b4a;color:#8ff0c0}
.j-disable{border-color:#6b3a3a;color:#ffb4b4}
.j-enable{border-color:#2b6b4a;color:#8ff0c0}
.j-runs{margin-left:auto;font-size:10px;color:#5f7c92}
.j-big-pause{margin-left:8px}
.j-blockers,.j-gates{background:#0a1420;border:1px solid #17324a;border-radius:8px;padding:14px;margin-top:18px}
.j-blockers li,.j-gates .gate-form{margin-bottom:10px}
.j-blockers ul{margin:8px 0 0;padding-left:18px}
.gate-form{display:flex;flex-wrap:wrap;gap:6px;align-items:center;border-top:1px solid #16324a;padding-top:8px}
.gate-form .who{color:#eaf6ff;min-width:180px}
.gate-btn{font:inherit;font-size:11px;padding:4px 10px;border-radius:4px;border:1px solid #2b4a63;background:#12283a;color:#c8d6e5;cursor:pointer}
.gate-btn.approve,.gate-btn.launch,.gate-btn.validated,.gate-btn.pay{border-color:#2b6b4a;color:#8ff0c0}
.gate-btn.reject,.gate-btn.rejected{border-color:#6b3a3a;color:#ffb4b4}
.gate-form input[type=text],.gate-form input[type=number]{background:#05080d;border:1px solid #2b4a63;color:#c8d6e5;padding:3px 6px;border-radius:4px;font:inherit}
.flash{padding:9px 14px;border-radius:6px;margin-bottom:16px;font-size:13px}
.flash.ok{background:#0f3d2a;color:#8ff0c0}.flash.err{background:#4a1220;color:#fca5a5}
.foot{margin-top:24px;color:#3f5a6c;font-size:11px}
button.voice,.voice{background:none;border:1px solid #2b4a63;color:#8fb3c8;border-radius:4px;padding:3px 8px;cursor:pointer;font:inherit}
.j-card.s-completed{border-left-color:#4ade80}
.j-card.s-failed{border-left-color:#f87171}.j-card.s-blocked{border-left-color:#fca5a5}
.j-card.s-human,.j-card.s-waiting{border-left-color:#fbbf24}
.j-name{text-decoration:none}
.j-deps{font-size:10px;color:#5f7c92;margin:2px 0 5px}
.j-deps .ok{color:#6ee7b7}.j-deps .no{color:#fca5a5}
.j-detail{font-size:10px;color:#5f7c92;text-decoration:none;border:1px solid #24384a;border-radius:4px;padding:3px 7px}
.j-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.chip{background:#0a1420;border:1px solid #17324a;border-radius:20px;padding:3px 11px;font-size:13px;color:#eaf6ff}
.chip small{color:#5f7c92;font-size:10px;letter-spacing:.08em}
.chip.hot{border-color:#7a5c1e;color:#ffd48a;background:#1c1608}
.cmdbar{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin:2px 0 18px;padding:10px;background:#08111c;border:1px solid #16324a;border-radius:8px}
.cmd-modes{margin-left:auto;font-size:10px;color:#5f7c92;letter-spacing:.14em;display:flex;gap:4px;align-items:center}
.j-mode{font:inherit;font-size:10px;letter-spacing:.1em;padding:3px 8px;border-radius:4px;border:1px solid #24384a;background:#0a1420;color:#7f9bb0;cursor:pointer}
.j-mode.sel{border-color:#38e8ff;color:#8ff0ff;background:#0b2733}
.j-stop{border-color:#7a2222;color:#fca5a5}
a.j-btn{text-decoration:none;display:inline-block}
.panel{background:#0a1420;border:1px solid #17324a;border-radius:8px;padding:14px;margin-bottom:14px}
.panel.accent{border-color:#22506b}
.panel h3{margin:0 0 10px;font-size:12px;letter-spacing:.14em;color:#8fb3c8}
.panel .note{color:#3f5a6c;font-size:10px;margin:8px 0 0}
.grid-h{font-size:12px;letter-spacing:.2em;color:#5f7c92;margin:20px 0 8px}
.rec{border-left:3px solid #2b4a63;padding:8px 10px;margin-bottom:8px;background:#08111c;border-radius:4px}
.rec.sev-critical{border-left-color:#f87171}.rec.sev-warning{border-left-color:#fbbf24}
.rec.sev-action{border-left-color:#38bdf8}.rec.sev-info{border-left-color:#3f5a6c}
.rec-h{display:flex;gap:8px;align-items:baseline}
.rec-sev{font-size:9px;letter-spacing:.1em;color:#5f7c92}
.rec p{margin:4px 0 0;font-size:12px;color:#9fb6c6}
.ha{border-top:1px solid #16324a;padding:9px 0}
.ha-h{display:flex;gap:7px;align-items:baseline;flex-wrap:wrap}
.ha-area{font-weight:600;color:#eaf6ff;letter-spacing:.06em}
.tag{font-size:9px;padding:1px 6px;border-radius:3px;background:#12283a;color:#8fb3c8}
.tag.money{background:#3a1220;color:#fca5a5}.tag.ext{background:#3a2a12;color:#ffd48a}
.tag.prep{background:#0f3d2a;color:#8ff0c0}.tag.st{background:#1c2b3a;color:#9fc3d8}
.ha-what{font-size:12px;color:#cfe0ea;margin:3px 0}
.ha-do{font-size:12px;color:#ffd48a}
.fs-row{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-top:1px solid #12283a;font-size:12px}
.fs-row:first-child{border-top:none}
.fs{font-size:11px;padding:1px 7px;border-radius:3px}
.fs.ok{background:#0f3d2a;color:#8ff0c0}.fs.bad{background:#4a1220;color:#fca5a5}
.fs.warn{background:#3a2a12;color:#ffd48a}
.ps-row{display:grid;grid-template-columns:90px 1fr 110px 220px;gap:10px;align-items:center;padding:6px 0;border-top:1px solid #12283a;font-size:12px}
.ps-row:first-child{border-top:none}
.ps-label{color:#8fb3c8;letter-spacing:.08em;font-size:11px}
.ps-track{position:relative;height:8px;background:#0f2130;border-radius:4px;overflow:visible}
.ps-mid{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:#24384a}
.ps-fill{position:absolute;top:0;bottom:0;border-radius:4px}
.ps-fill.pos{left:50%;background:#34d399}
.ps-fill.neg{right:50%;background:#fca5a5}
.ps-val{font-weight:600}
.ps-val.pos{color:#6ee7b7}.ps-val.neg{color:#fca5a5}
.ps-sub{color:#5f7c92;font-size:11px}
.rp{display:flex;gap:9px;align-items:baseline;padding:4px 0;font-size:12px}
.rp-stage{min-width:100px;color:#eaf6ff;letter-spacing:.06em}
.rp-note{color:#8fa8b8}
.rp-red .rp-note{color:#fca5a5}
.psteps{margin-top:8px}
.pstep{display:grid;grid-template-columns:20px 150px 130px 1fr;gap:8px;align-items:baseline;padding:4px 0;font-size:12px;border-top:1px solid #101f2c}
.pstep:first-child{border-top:none}
.pg{text-align:center}
.pcap{color:#eaf6ff}.pagent{color:#5f7c92;font-size:11px}
.p-ok .pstat{color:#6ee7b7}.p-running .pstat{color:#7dd3fc}.p-blocked .pstat,.p-failed .pstat{color:#fca5a5}
.p-skipped{opacity:.6}.p-pending{opacity:.55}
.acq-sum{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:#8fa8b8;margin-bottom:10px}
.acq-sum b{color:#eaf6ff}
.lead{display:grid;grid-template-columns:44px 1fr 200px 220px auto;gap:8px;align-items:baseline;padding:6px 0;border-top:1px solid #101f2c;font-size:11.5px}
.lead-score{color:#8ff0c0;font-weight:600}
.lead-sig{color:#cfe0ea;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lead-meta{color:#5f7c92}.lead-rec{color:#ffd48a}
.feed{max-height:280px;overflow-y:auto;font-size:11px}
.ev{display:grid;grid-template-columns:120px 60px 1fr;gap:8px;padding:3px 0;border-top:1px solid #101f2c}
.ev-t{color:#5f7c92}.ev-k{color:#7f9bb0}.ev-x{color:#c8d6e5}
.jout{background:#05080d;border:1px solid #16324a;border-radius:6px;padding:10px;overflow-x:auto;font-size:11px;color:#9fb6c6;max-height:360px}
.big.holding{color:#fbbf24}.big.online{color:#4ade80}
.swap{display:contents}
/* ---- the ecosystem ---- */
.eco-wrap{background:radial-gradient(120% 90% at 50% 15%,#0c1a2b,#060d17);
 border:1px solid #16324a;border-radius:12px;padding:6px;overflow-x:auto}
.eco-svg{width:100%;min-width:760px;height:auto;display:block}
.eco-zone ellipse{fill:#ffffff05;stroke:#ffffff10;stroke-dasharray:3 5}
.eco-zone text{fill:#ffffff33;font-size:11px;letter-spacing:.22em;text-anchor:middle}
.eco-edge{fill:none;stroke-linecap:round}
.eco-edge.e-wire{stroke:#ffffff14;stroke-width:1.5}
.eco-edge.e-flow{stroke:#7fd7ff88;stroke-width:2;stroke-dasharray:4 7;
 animation:eco-march 1.6s linear infinite}
.eco-edge.e-live{stroke:#8ff0c0;stroke-width:2.8;stroke-dasharray:5 6;
 filter:drop-shadow(0 0 5px #8ff0c0);animation:eco-march .55s linear infinite}
@keyframes eco-march{to{stroke-dashoffset:-22}}
.eco{--bob:0s}
.eco .eco-shadow{fill:#00000055}
.eco .eco-ring{fill:none;stroke:var(--acc);stroke-width:2;opacity:.35}
.eco .eco-body{fill:var(--acc);
 filter:drop-shadow(0 3px 6px #0008) drop-shadow(0 0 10px color-mix(in srgb,var(--acc) 50%,transparent))}
.eco .eco-icon{color:#fff;stroke:#fff}
.eco .eco-icon svg{width:24px;height:24px;filter:drop-shadow(0 1px 1px #0007)}
.eco .eco-pupil{fill:#0b1a2b}
.eco .eco-name{fill:#c8d6e5;font-size:9px;text-anchor:middle;letter-spacing:.02em}
/* ambient wander sets the g's transform attribute every frame (_JS ecoFrame);
   NO css transform / transform-box on .eco - it must not shadow the attribute.
   With JS off the creatures sit at their server-rendered home positions. */
.eco-body,.eco-ring{transform-box:fill-box;transform-origin:center}
.eco.m-work .eco-body{animation:eco-work .5s ease-in-out infinite}
.eco.m-work .eco-ring{animation:eco-pulse 1.1s ease-out infinite;opacity:.6}
@keyframes eco-work{0%,100%{transform:scale(1) rotate(-3deg)}50%{transform:scale(1.08) rotate(3deg)}}
@keyframes eco-pulse{0%{r:20;opacity:.6}100%{r:32;opacity:0}}
.eco.m-sleep{opacity:.4;filter:grayscale(1)}
.eco.m-sleep .eco-smile{d:path('M-5 7 Q0 4 5 7')}
.eco.m-wait .eco-body{stroke:#fbbf24;stroke-width:2}
.eco.m-wait .eco-smile{d:path('M-5 7 L5 7')}
.au-reason{margin:8px 0;font-size:12px;color:#9fb6c6;font-style:italic}
.au-bl{margin:6px 0 0;padding-left:18px;font-size:12px;color:#ffb4b4}
.opp-board{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.opp-col{background:#08111c;border:1px solid #14293b;border-radius:6px;padding:8px}
.opp-h{font-size:9px;letter-spacing:.1em;color:#5f7c92;margin-bottom:5px}
.opp-col ul{margin:0;padding-left:14px;font-size:10.5px;color:#b8ccd8}
.opp-col li{margin-bottom:3px}
.opp-col li.more{color:#5f7c92;list-style:none;margin-left:-14px}
.oc-successful{border-color:#2b6b4a}.oc-abandoned{opacity:.55}
.oc-testing,.oc-active{border-color:#7a5c1e}
.xrow{border-top:1px solid #16324a;padding:9px 0;margin-top:6px}
.xrow-h{display:flex;justify-content:space-between;align-items:center;gap:8px}
.xstate{font-size:9px;letter-spacing:.08em;color:#8ff0c0;border:1px solid #2b6b4a;border-radius:4px;padding:1px 6px;white-space:nowrap}
.xchips{margin:6px 0;display:flex;flex-wrap:wrap;gap:4px}
.xtask{font-size:9.5px;padding:1px 6px;border-radius:9px;border:1px solid #14293b;color:#9fb6c6}
.xtask.x-succeeded{border-color:#2b6b4a;color:#8ff0c0}
.xtask.x-running{border-color:#7a5c1e;color:#ffd98a}
.xtask.x-ready{border-color:#2f5a7a;color:#bfe0ff}
.xtask.x-blocked_approval{border-color:#7a2e2e;color:#ffb4b4}
.xtask.x-failed_final,.xtask.x-cancelled{border-color:#7a2e2e;color:#c98a8a;text-decoration:line-through}
.xmeta{font-size:10.5px;color:#8fa8ba}
.x-blk{font-size:10.5px;color:#ffb4b4;margin-top:3px}
.xcand{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:11px;color:#b8ccd8;padding:4px 0;border-top:1px solid #0e2131}
.xcand small{color:#5f7c92}
.x-h{font-size:9px;letter-spacing:.1em;color:#5f7c92;margin:10px 0 2px}
.x-note{font-size:10px;color:#5f7c92;margin-left:8px}
.appr{border-top:1px solid #16324a;padding:9px 0;margin-top:6px}
.appr-k{font-weight:600;letter-spacing:.08em;color:#eaf6ff;margin-top:10px}
.appr-what{color:#eaf6ff;font-size:13px}
.appr-why,.appr-x,.appr-after{font-size:11.5px;color:#9fb6c6;margin-top:3px}
.appr-after{color:#8ff0c0}
.j-mode.sel{border-color:#38e8ff;color:#8ff0ff;background:#0b2733}
"""

_JS = """
(function(){
 function busy(){
  if(document.hidden)return true;
  var a=document.activeElement;
  if(a&&/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))return true;
  return false;
 }
 function soft(){
  if(busy())return;
  var u=location.pathname+(location.search?location.search+'&partial=1':'?partial=1');
  fetch(u,{headers:{'X-Requested-With':'jarvis'}})
   .then(function(r){return r.text()}).then(function(t){
     var d=new DOMParser().parseFromString(t,'text/html');
     var swaps=d.querySelectorAll('.swap'), n=0;
     swaps.forEach(function(f){
       if(f.id==='p-eco'){return;}   /* JS-animated - reconciled, never nuked */
       var cur=document.getElementById(f.id);
       if(cur && cur.innerHTML!==f.innerHTML){cur.innerHTML=f.innerHTML; n++;}
     });
     syncEco(d);
     var fs=d.getElementById('says'), cs=document.getElementById('says');
     if(fs&&cs&&fs.getAttribute('data-say')!==cs.getAttribute('data-say')){
       cs.setAttribute('data-say',fs.getAttribute('data-say'));
       cs.textContent='\\u201c'+(fs.textContent||'').replace(/[\\u201c\\u201d]/g,'')+'\\u201d';
     }
     wireVoice();
   }).catch(function(){});
 }
 function wireVoice(){
  var s=document.getElementById('says'); if(!s)return;
  var say=s.getAttribute('data-say')||'';
  if(localStorage.getItem('jarvisVoice')==='on' && say && window.speechSynthesis){
    if(say!==window.__lastSay){window.__lastSay=say;
      var u=new SpeechSynthesisUtterance(say);u.rate=1.0;u.pitch=.9;
      window.speechSynthesis.cancel();window.speechSynthesis.speak(u);}
  }
 }
 window.jarvisVoiceToggle=function(btn){
  var on=localStorage.getItem('jarvisVoice')==='on';
  localStorage.setItem('jarvisVoice',on?'off':'on');
  btn.textContent=on?'\\u25b6 voice off':'\\u25a0 voice on';
  if(!on){window.__lastSay='';wireVoice();}
 };
 /* ---- ecosystem: ambient real-time wander --------------------------- */
 var ECO_P={};
 function ecoParams(id){
  if(ECO_P[id])return ECO_P[id];
  var h=2166136261;
  for(var i=0;i<id.length;i++){h=(h^id.charCodeAt(i))>>>0;h=(h*16777619)>>>0;}
  function rnd(n){h=(h*1664525+1013904223)>>>0;return h/4294967296*n;}
  ECO_P[id]={ax:16+rnd(16),ay:11+rnd(11),fx:0.22+rnd(0.20),fy:0.18+rnd(0.16),
             px:rnd(6.283),py:rnd(6.283),f2:0.5+rnd(0.6),p2:rnd(6.283)};
  return ECO_P[id];
 }
 function ecoFrame(ts){
  try{
   var svg=document.querySelector('.eco-svg');
   var s=(typeof ts==='number'?ts:0)/1000;
   if(svg){
    var pos={}, cr=svg.querySelectorAll('.eco'), i;
    for(i=0;i<cr.length;i++){
     var el=cr[i], id=el.getAttribute('data-id')||'';
     var hx=parseFloat(el.getAttribute('data-hx')); if(!isFinite(hx))hx=0;
     var hy=parseFloat(el.getAttribute('data-hy')); if(!isFinite(hy))hy=0;
     var cls=el.getAttribute('class')||'';
     var amp=cls.indexOf('m-sleep')>=0?0.2:cls.indexOf('m-work')>=0?1.5:1;
     var p=ecoParams(id);
     var ox=amp*(p.ax*Math.sin(s*p.fx+p.px)+p.ax*0.4*Math.sin(s*p.f2+p.p2));
     var oy=amp*(p.ay*Math.sin(s*p.fy+p.py)+4*Math.sin(s*0.8+p.px));
     var nx=hx+ox, ny=hy+oy;
     if(!isFinite(nx)||!isFinite(ny)){nx=hx;ny=hy;}
     el.setAttribute('transform','translate('+nx.toFixed(1)+' '+ny.toFixed(1)+')');
     pos[id]=[nx,ny];
    }
    var ed=svg.querySelectorAll('.eco-edge');
    for(i=0;i<ed.length;i++){
     var e=ed[i], a=pos[e.getAttribute('data-from')], b=pos[e.getAttribute('data-to')];
     if(!a||!b)continue;
     var mx=(a[0]+b[0])/2, my=(a[1]+b[1])/2-26;
     e.setAttribute('d','M'+a[0].toFixed(1)+' '+a[1].toFixed(1)+' Q'+mx.toFixed(1)+' '
       +my.toFixed(1)+' '+b[0].toFixed(1)+' '+b[1].toFixed(1));
    }
   }
   window.__ecoN=(window.__ecoN||0)+1;
  }catch(err){ window.__ecoErr=String(err&&err.message||err); }
  window.__ecoRAF=requestAnimationFrame(ecoFrame);
 }
 function startEco(){
  if(window.__ecoOn||!window.requestAnimationFrame)return;
  window.__ecoOn=true; window.__ecoRAF=requestAnimationFrame(ecoFrame);
 }
 /* keep the animated creatures alive across a soft refresh: update only
    their mood class + the edge set, never their positions. */
 function syncEco(doc){
  var ns=doc&&doc.querySelector('.eco-svg'), cs=document.querySelector('.eco-svg');
  if(!ns||!cs)return;
  ns.querySelectorAll('.eco').forEach(function(nn){
   var cur=cs.querySelector('.eco[data-id="'+nn.getAttribute('data-id')+'"]');
   if(cur)cur.setAttribute('class',nn.getAttribute('class'));
  });
  var anchor=cs.querySelector('.eco');
  cs.querySelectorAll('.eco-edge').forEach(function(x){x.parentNode.removeChild(x);});
  ns.querySelectorAll('.eco-edge').forEach(function(x){
   cs.insertBefore(document.importNode(x,true),anchor);
  });
 }
 function initJarvis(){
  var b=document.getElementById('voiceBtn');
  if(b)b.textContent=localStorage.getItem('jarvisVoice')==='on'?'\\u25a0 voice on':'\\u25b6 voice off';
  wireVoice(); startEco(); setInterval(soft,6000);
  setInterval(function(){
   var d=document.getElementById('eco-dbg'); if(!d)return;
   var svg=document.querySelector('.eco-svg');
   var n=svg?svg.querySelectorAll('.eco').length:-1;
   d.textContent=' · anim '+(window.__ecoN||0)+' frames, '+n+' creatures'
     +(window.__ecoErr?(' · ERR: '+window.__ecoErr):'');
  },1000);
 }
 if(document.readyState==='loading')
   document.addEventListener('DOMContentLoaded',initJarvis);
 else initJarvis();
 startEco();
})();
"""


def _tok(csrf: str, **hidden) -> str:
    h = f"<input type=hidden name=csrf value='{_esc(csrf)}'>"
    for k, v in hidden.items():
        h += f"<input type=hidden name={k} value='{_esc(str(v))}'>"
    return h


def _cbtn(csrf: str, action: str, label: str, cls: str, *, hidden=None,
         disabled=False, title="") -> str:
    d = " disabled" if disabled else ""
    t = f' title="{_esc(title)}"' if title else ""
    return (f"<form method=post action=/control class=j-inline>"
            f"{_tok(csrf, **(hidden or {}))}"
            f"<button class='{cls}' name=action value={action}{d}{t}>{label}</button></form>")


def _command_bar(snap: dict, csrf: str) -> str:
    busy = bool((snap.get("job") or {}).get("running"))
    paused = snap["paused"]
    mode = snap.get("mode", "manual")
    batch_ok = (mode == "auto") and not paused and not busy
    why_no_batch = ("switch to AUTO mode to run batch operations" if mode != "auto"
                    else "a job is running" if busy else "fleet is paused")
    async_h = {"mode": "async", "restart": "1"}

    auto_ok = not paused and not busy
    pieces = [
        _cbtn(csrf, "run-autonomy", "🤖 RUN AUTONOMY CYCLE", "j-btn j-run",
              disabled=not auto_ok,
              title="" if auto_ok else ("fleet is paused" if paused else "a job is running")),
        _cbtn(csrf, "run-sweep", "▶ RUN FLEET", "j-btn j-run", hidden=async_h,
              disabled=not batch_ok, title="" if batch_ok else why_no_batch),
        _cbtn(csrf, "run-pipeline", "▶ RUN PIPELINE", "j-btn j-run", hidden=async_h,
              disabled=not batch_ok, title="" if batch_ok else why_no_batch),
    ]
    if paused:
        pieces.append(_cbtn(csrf, "resume", "▶ RESUME ALL", "j-btn j-enable"))
    else:
        pieces.append(_cbtn(csrf, "pause", "⏸ PAUSE ALL", "j-btn j-disable"))
    if busy:
        pieces.append(_cbtn(csrf, "stop-job", "■ STOP JOB", "j-btn j-stop"))
    pieces.append("<a class='j-btn' href='/'>🔄 REFRESH</a>")

    modes = ""
    for m in ("manual", "auto", "autonomous", "paused"):
        sel = " sel" if mode == m else ""
        modes += (f"<form method=post action=/control class=j-inline>{_tok(csrf, mode=m)}"
                  f"<button class='j-mode{sel}' name=action value=set-mode>{m.upper()}"
                  f"</button></form>")

    voice = ("<button id=voiceBtn class=voice type=button "
             "onclick='jarvisVoiceToggle(this)'>&#9654; voice off</button>")
    return (f"<div class=cmdbar>{''.join(pieces)}{voice}"
            f"<span class=cmd-modes>FLEET MODE {modes}</span></div>")


def render_console(data_dir, *, flash: str | None = None,
                   csrf: str | None = None, partial: bool = False,
                   agent: str | None = None) -> str:
    snap = jarvis_snapshot(data_dir)
    csrf = csrf or ""

    if agent:
        return _wrap(_render_agent_detail(snap, data_dir, agent, csrf, flash), partial)

    line = _jarvis_line(snap)
    busy = bool((snap.get("job") or {}).get("running"))

    by_cluster: dict[str, list] = {c: [] for c in roster.CLUSTERS}
    for a in snap["agents"]:
        by_cluster.setdefault(a["cluster"], []).append(a)
    grid = ""
    for cl in roster.CLUSTERS:
        grid += f"<div class=j-cluster>{cl.upper()}</div>"
        grid += "".join(_agent_card(a, csrf, busy) for a in by_cluster.get(cl, []))

    flash_html = ""
    if flash:
        kind = "ok" if flash.startswith("ok") else "err"
        flash_html = f"<div class='flash {kind}'>{_esc(flash)}</div>"

    st = snap["status"]
    reactor_cls = ("reactor off" if st == "PAUSED"
                   else "reactor busy" if st == "WORKING" else "reactor")
    sys_cls = "holding" if st in ("PAUSED", "HOLDING") else "online"
    top = (
        "<div class=j-top>"
        f"<div class='{reactor_cls}'></div>"
        "<div class=j-title><b>J A R V I S</b><small>local agent command center</small></div>"
        f"<div class=j-sys><div class='big {sys_cls}'>{st}</div>"
        f"<small>{_esc(snap['generated_at'][:19].replace('T', ' '))} UTC · "
        f"mode {snap.get('mode', 'manual').upper()}</small></div>"
        "</div>"
    )

    def _p(pid, html):
        return f"<div id={pid} class=swap>{html}</div>"

    main = (
        f"<main id=j-main>"
        f"{_p('p-top', top)}"
        f"{flash_html}"
        f"<p id=says data-say=\"{_esc(_voice_line(snap))}\">“{_esc(line)}”</p>"
        f"{_p('p-vitals', _vitals(snap))}"
        f"{_p('p-cmd', _command_bar(snap, csrf))}"
        f"{_p('p-appr', _approvals_panel((snap.get('autonomy') or {}).get('pending') or {}, csrf))}"
        f"{_p('p-auto', _autonomy_panel(snap.get('autonomy') or {}))}"
        f"{_p('p-oppb', _opportunity_board_panel((snap.get('autonomy') or {}).get('board') or {}))}"
        f"{_p('p-exec', _execution_panel(snap.get('execution') or [], (snap.get('autonomy') or {}).get('board') or {}, csrf))}"
        f"{_p('p-recs', _recommends_panel(snap.get('recommendations') or []))}"
        f"{_p('p-human', _human_actions_panel(snap.get('human_actions') or [], csrf))}"
        f"{_p('p-pipe', _pipeline_panel(snap['pipeline'], snap.get('job') or {}))}"
        f"{_p('p-rev', _revenue_pipeline_panel(snap.get('revenue_pipeline') or []))}"
        f"{_p('p-fin', _financial_panel(snap.get('financial') or {}))}"
        f"{_p('p-profit', _profit_scale_panel(snap.get('profit_scale') or {}))}"
        f"{_p('p-llm', _llm_budget_panel(snap.get('llm') or {}))}"
        f"{_p('p-blk', _blockers_panel(snap['blockers'], csrf))}"
        f"{_p('p-gates', _gates_panel(snap['action_queue'], csrf))}"
        f"{_p('p-out', _outreach_panel(snap.get('outreach') or [], csrf))}"
        f"{_p('p-acq', _acquisition_panel(snap.get('acquisition') or {}, csrf))}"
        f"{_p('p-eco', _ecosystem_panel(_ecosystem_data(snap)))}"
        f"<h2 class=grid-h>THE FLEET — 24 agents (cards)</h2>"
        f"{_p('p-grid', f'<div class=j-grid>{grid}</div>')}"
        f"{_p('p-feed', _activity_feed_panel(snap.get('events') or []))}"
        f"</main>"
    )
    return _wrap(main, partial)


def _wrap(main: str, partial: bool) -> str:
    if partial:
        return f"<!doctype html><html><body>{main}</body></html>"
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>JARVIS — Agent Command</title>"
        "<noscript><meta http-equiv=refresh content=6></noscript>"
        f"<style>{_CSS}</style></head><body><div class=wrap>"
        f"{main}"
        "<div class=foot>Every action routes through the same tested domain "
        "functions the CLI uses. JARVIS never moves money, spends, publishes, "
        "deploys, posts, emails, or calls an LLM — those stay human gates.</div>"
        f"</div><script>{_JS}</script></body></html>"
    )


def _render_agent_detail(snap: dict, data_dir, agent_id: str, csrf: str,
                         flash: str | None) -> str:
    a = next((x for x in snap["agents"] if x["id"] == agent_id), None)
    if a is None:
        return "<main id=j-main><p><a href='/'>&larr; back</a> — unknown agent.</p></main>"
    flash_html = ""
    if flash:
        k = "ok" if flash.startswith("ok") else "err"
        flash_html = f"<div class='flash {k}'>{_esc(flash)}</div>"

    tok = _tok(csrf, agent=a["id"])
    toggle_a = "disable" if a["enabled"] else "enable"
    toggle = (f"<form method=post action=/control class=j-inline>{tok}"
              f"<button class='j-btn j-{toggle_a}' name=action value={toggle_a}>"
              f"{toggle_a.title()}</button></form>")
    if a["runnable_here"] and a["can_run_now"]:
        run = (f"<form method=post action=/control class=j-inline>{tok}"
               f"<button class='j-btn j-run' name=action value=run>Run now</button></form>")
        run_note = ""
    else:
        run = "<button class='j-btn j-run' disabled>Run now</button>"
        run_note = (f"<p class=j-hint><b>Run now is unavailable:</b> "
                    f"{_esc(a['run_blocked_reason'] or 'not runnable from JARVIS')}</p>")

    deps = a.get("dependencies") or []
    dep_html = ("<ul>" + "".join(
        f"<li>{'✓' if d['met'] else '○'} {_esc(d['name'])} "
        f"<small>({'output present' if d['met'] else 'no output yet'})</small></li>"
        for d in deps) + "</ul>") if deps else "<p>none</p>"

    out = load_agent_outputs(data_dir).get(a["capability"])
    hist = a.get("history") or []
    rows = [
        ("Identity", f"{_esc(a['name'])} · <span class=mono>{_esc(a['id'])}</span>"),
        ("Purpose", _esc(a.get("description", ""))),
        ("Cluster", _esc(a["cluster"])),
        ("Role", _esc(a["role"])),
        ("Human-gated", "YES — " + _esc(a.get("next_step_hint", "a human must act"))
         if a["human_gated"] else "no"),
        ("Current state", _STATE_LABEL.get(a["state"], a["state"].upper())),
        ("Progress", f"{a['progress']}% — {_esc(a['progress_label'])}"),
        ("Runs recorded", str(a["runs"])),
        ("Last activity", _esc(a["last"] or "none")
         + (f" → {_esc(a['last_result'])}" if a.get("last_result") else "")),
        ("Related pipeline step", _esc(a.get("pipeline_step") or "— not in the pipeline")),
    ]
    if a["why_waiting"]:
        rows.append(("Why waiting for human", _esc(a["why_waiting"])))
    body = "".join(f"<div class=fs-row><span>{k}</span><span>{v}</span></div>"
                   for k, v in rows)

    latest = ""
    if isinstance(out, dict):
        import json as _json
        latest = ("<h3>Latest output</h3><pre class=jout>"
                  + _esc(_json.dumps(out.get("output", {}), indent=2)[:3000]) + "</pre>")

    hist_html = ("<h3>Execution history</h3><ul>" + "".join(
        f"<li>{_esc(h['ts'])} · {_esc(h['capability'])} · {_esc(h['status'] or 'ok')} "
        f"<small>{_esc(h['objective'])}</small></li>" for h in hist) + "</ul>"
        ) if hist else "<h3>Execution history</h3><p>no recorded runs</p>"

    return (
        f"<main id=j-main><p><a href='/'>&larr; back to command center</a></p>"
        f"{flash_html}"
        f"<section class=panel>{_agent_avatar(a['id'])}"
        f"<h2 style='display:inline'>{_esc(a['name'])}</h2>"
        f"{body}"
        f"<div class=j-actions>{toggle}{run}</div>{run_note}"
        f"<h3>Dependencies</h3>{dep_html}"
        f"{hist_html}"
        f"{latest}"
        f"</section></main>"
    )


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

def _make_handler(data_dir: Path, actor: str, csrf: str, allowed_origins):

    class Handler(BaseHTTPRequestHandler):
        server_version = "RevenueOS-jarvis"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or []):
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _same_origin(self):
            for h in ("Origin", "Referer"):
                val = self.headers.get(h)
                if val:
                    host = urlsplit(val).netloc.split("@")[-1].lower()
                    if host not in allowed_origins:
                        return False
            return True

        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path != "/":
                self._send(404, b"not found")
                return
            q = parse_qs(parts.query or "")
            partial = (q.get("partial") or [""])[0] == "1"
            agent = (q.get("agent") or [None])[0]
            flash = self.server._flash
            self.server._flash = None
            html = render_console(data_dir, flash=flash, csrf=csrf,
                                  partial=partial, agent=agent).encode("utf-8")
            self._send(200, html)

        def do_POST(self):
            if urlsplit(self.path).path not in ("/control", "/action"):
                self._send(404, b"not found")
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0 or length > _MAX_BODY:
                self._send(400, b"bad request")
                return
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            if (form.get("csrf") or [""])[0] != csrf:
                self._send(403, b"bad csrf token")
                return
            if not self._same_origin():
                self._send(403, b"cross-origin blocked")
                return
            self.server._flash = apply_control(data_dir, actor, form)
            self._send(303, b"", extra=[("Location", "/")])

    return Handler


def build_server(data_dir, host: str = "127.0.0.1", port: int = 8788,
                 actor: str = "jarvis"):
    """Construct the (httpd, csrf) pair without serving. Loopback only."""
    data_dir = Path(data_dir)
    if host not in _LOOPBACK:
        raise ValueError(
            f"refusing to bind to non-loopback host {host!r}; JARVIS is localhost-only"
        )
    csrf = secrets.token_urlsafe(24)
    bind = "127.0.0.1" if host == "localhost" else host
    httpd = ThreadingHTTPServer((bind, port), None)
    real_port = httpd.server_address[1]
    allowed = {f"127.0.0.1:{real_port}", f"localhost:{real_port}", f"[::1]:{real_port}"}
    httpd.RequestHandlerClass = _make_handler(data_dir, actor, csrf, allowed)
    httpd._flash = None
    return httpd, csrf


_AUTONOMY_INTERVAL = 45   # seconds between autonomous cycles while in AUTONOMOUS mode


def _autonomy_watcher(data_dir, stop_evt) -> None:
    """While the fleet is in AUTONOMOUS mode (and not paused, and no job is
    running) kick one autonomous cycle every _AUTONOMY_INTERVAL seconds.
    This is the REAL continuous loop - not a label."""
    from .agent_control import load_agent_control

    while not stop_evt.wait(_AUTONOMY_INTERVAL):
        try:
            ctrl = load_agent_control(data_dir)
            if ctrl.mode != "autonomous" or ctrl.is_paused():
                continue
            with _JOB_LOCK:
                if _JOB["running"]:
                    continue
            _start_job("autonomy loop", _autonomy_job, data_dir, 1)
        except Exception:
            logger.exception("autonomy watcher tick failed")


def serve(data_dir, host: str = "127.0.0.1", port: int = 8788,
          actor: str = "jarvis") -> None:
    httpd, _ = build_server(data_dir, host=host, port=port, actor=actor)
    real_port = httpd.server_address[1]
    stop_evt = threading.Event()
    threading.Thread(target=_autonomy_watcher, args=(Path(data_dir), stop_evt),
                     name="jarvis-autonomy-watcher", daemon=True).start()
    print(f"JARVIS: http://localhost:{real_port}/  (actor={actor}, Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        httpd.server_close()
