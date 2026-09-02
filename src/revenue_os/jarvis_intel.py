"""Deterministic intelligence for the JARVIS command center.

Pure functions over already-loaded state - no I/O, no LLM, no network,
no randomness. Given the same inputs they always return the same output.
The caller (jarvis_server) reads the repository state and passes it in.

They answer, from real state only:
  * recommendations()   - what should happen next, ranked by severity
  * human_actions()     - every pending human gate, spelled out
  * financial_safety()  - is the system allowed to spend money right now
  * revenue_pipeline()  - LEAD -> ... -> REVENUE, each stage's real state
  * acquisition_view()  - lead supply + per-lead recommended action
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# recommendations - "JARVIS RECOMMENDS"
# ---------------------------------------------------------------------------

_SEV_RANK = {"critical": 0, "warning": 1, "action": 2, "info": 3}


def recommendations(snap: dict) -> list[dict]:
    """Ranked next-action list. Each item:
    {severity, title, detail, cta_action?, cta_label?}. Deterministic."""
    out: list[dict] = []
    blockers = snap.get("blockers") or []
    fin = snap.get("financial") or {}
    acq = snap.get("acquisition") or {}
    counts = snap.get("counts") or {}
    pipe = snap.get("pipeline") or {}
    job = snap.get("job") or {}
    agents = snap.get("agents") or []

    # 1) a running job trumps everything - say what is happening
    if job.get("running"):
        step = pipe.get("current_step") or "the fleet"
        out.append({
            "severity": "info",
            "title": f"Fleet is processing: {step}",
            "detail": f"A {job.get('what', 'job')} is running. "
                      f"{pipe.get('done', 0)} of {pipe.get('total', 0)} pipeline "
                      f"steps complete. Nothing else should be started until it finishes.",
        })
        return out

    if snap.get("paused"):
        out.append({
            "severity": "warning",
            "title": "Fleet is PAUSED - nothing will run",
            "detail": snap.get("paused_reason") or "The global pause is on.",
            "cta_action": "resume", "cta_label": "Resume all",
        })

    # 2) real financial blockers first
    for b in blockers:
        if b.get("area") == "payment" or "PAYPAL" in str(b.get("id", "")).upper() \
                or "PAYEE" in str(b.get("detail", "")).upper():
            out.append({
                "severity": "critical",
                "title": "PayPal is preventing customer payments",
                "detail": (f"{b.get('title', 'PayPal blocked')} "
                           f"({b.get('id', '')}). {b.get('detail', '')} "
                           "This is an account-level PayPal restriction - it "
                           "cannot be fixed from code. "
                           "Next human action: open PayPal and resolve the "
                           "account restriction."),
            })
            break

    # 3) other open blockers
    others = [b for b in blockers
              if b.get("area") != "payment"
              and "PAYEE" not in str(b.get("detail", "")).upper()]
    for b in others:
        out.append({
            "severity": "warning",
            "title": f"Open blocker: {b.get('title', b.get('id', ''))}",
            "detail": (b.get("detail", "") + "  Human action: "
                       "resolve it, then clear it in JARVIS."),
        })

    # 4) lead supply
    total_leads = acq.get("total", 0)
    fresh = acq.get("fresh", 0)
    if total_leads == 0:
        out.append({
            "severity": "action",
            "title": "No acquisition leads",
            "detail": "Lead supply is empty. Next action: run discovery from the "
                      "CLI (`revenue_os discover-free` - free, no LLM).",
        })
    elif fresh == 0:
        out.append({
            "severity": "action",
            "title": "All leads are stale",
            "detail": f"{total_leads} lead(s) on file, none fresh (< 7 days). "
                      "Posting to old threads rarely converts. Next action: run "
                      "discovery for fresh ones.",
        })

    # 5) pipeline state
    pstatus = pipe.get("status")
    if pstatus == "blocked":
        gate = pipe.get("human_gate") or {}
        out.append({
            "severity": "warning",
            "title": "Pipeline is blocked",
            "detail": gate.get("reason") or "A pipeline step is blocked - see the "
                      "pipeline panel for the exact step.",
        })
    elif pstatus in ("failed", "stopped"):
        out.append({
            "severity": "warning",
            "title": f"Pipeline {pstatus}",
            "detail": (pipe.get("error") or "See the pipeline panel.")
            + "  Fix the cause, then Run Pipeline again.",
        })
    elif pstatus == "prepared":
        gate = pipe.get("human_gate") or {}
        nxt = gate.get("human_gated_next") or []
        out.append({
            "severity": "action",
            "title": "Pipeline is prepared - waiting on you",
            "detail": (gate.get("reason") or "QC passed.")
            + (f"  Next human steps: {'; '.join(nxt[:2])}" if nxt else ""),
        })

    # 6) human gates that JARVIS has NOT yet prepared a draft for
    undrafted = [a for a in agents
                 if a.get("human_gated") and a.get("runnable_here")
                 and not a.get("has_draft") and a.get("can_run_now")]
    if undrafted:
        names = ", ".join(a["name"] for a in undrafted[:3])
        out.append({
            "severity": "action",
            "title": f"{len(undrafted)} human-gated agent(s) have no draft yet",
            "detail": f"JARVIS can prepare the spec/draft for: {names}. "
                      "Run each (draft only - nothing ships).",
        })

    # 7) idle fleet
    running = counts.get("running", 0)
    if not out and running == 0:
        if pstatus in (None, "idle"):
            out.append({
                "severity": "action",
                "title": "Fleet is idle",
                "detail": "Nothing is running and nothing is waiting on you. "
                          "Recommended: switch to AUTO and Run Fleet, or Run "
                          "Pipeline for the qualified candidate.",
                "cta_action": "run-sweep", "cta_label": "Run Fleet",
            })

    if not out:
        out.append({
            "severity": "info",
            "title": "Nothing urgent",
            "detail": "No blockers, no running job, no pipeline gate. Review the "
                      "Human Actions panel for anything outstanding.",
        })

    out.sort(key=lambda r: _SEV_RANK.get(r["severity"], 9))
    return out


# ---------------------------------------------------------------------------
# human actions - the dedicated "HUMAN ACTIONS" section
# ---------------------------------------------------------------------------

def human_actions(snap: dict) -> list[dict]:
    """Every pending human gate, spelled out. Each item:
    {area, status, what, why, human_action, jarvis_can_prepare, affects_money}."""
    items: list[dict] = []

    for b in (snap.get("blockers") or []):
        money = b.get("area") == "payment" or "PAYEE" in str(b.get("detail", "")).upper()
        items.append({
            "area": (b.get("area") or "blocker").upper(),
            "status": "BLOCKED",
            "what": b.get("title", b.get("id", "")),
            "why": b.get("detail", ""),
            "human_action": ("Open PayPal and resolve the account restriction."
                             if money else
                             "Resolve the underlying issue, then clear it in JARVIS."),
            "jarvis_can_prepare": False,
            "affects_money": bool(money),
        })

    for a in (snap.get("agents") or []):
        if not a.get("human_gated"):
            continue
        if a.get("gate_acknowledged"):
            continue
        why = a.get("why_waiting") or ""
        if not why and not a.get("has_draft"):
            continue                      # nothing pending for this agent
        money = a["id"] in ("ads_manager", "campaign_optimizer", "budget_allocator")
        external = a["id"] in ("store_builder", "outreach_drafter", "developer",
                               "automation_engineer")
        items.append({
            "area": a["name"].upper(),
            "status": "WAITING FOR HUMAN",
            "what": why or "a draft is ready for your review",
            "why": a.get("next_step_hint") or why,
            "human_action": a.get("next_step_hint")
            or "Review the draft; perform the real step yourself.",
            "jarvis_can_prepare": bool(a.get("runnable_here")),
            "affects_money": bool(money),
            "affects_external": bool(external or money),
            "agent_id": a["id"],
            "has_draft": bool(a.get("has_draft")),
        })

    for q in (snap.get("action_queue") or []):
        items.append({
            "area": "CANDIDATE",
            "status": "WAITING FOR HUMAN",
            "what": f"{q.get('name', '')}: {q.get('next_action', '')}",
            "why": "a lifecycle decision only a human makes",
            "human_action": q.get("next_action", "decide in the Candidate gates panel"),
            "jarvis_can_prepare": False,
            "affects_money": q.get("status") in ("launched", "earning"),
        })

    for o in (snap.get("outreach") or []):
        items.append({
            "area": "OUTREACH",
            "status": "WAITING FOR HUMAN",
            "what": o.get("title", o.get("lead_id", "")),
            "why": "a reply draft is prepared; the system never posts",
            "human_action": "Review the prepared message and manually post it, "
                            "then log posted/skipped.",
            "jarvis_can_prepare": True,
            "affects_external": True,
            "affects_money": False,
        })

    return items


# ---------------------------------------------------------------------------
# financial safety
# ---------------------------------------------------------------------------

def financial_safety(*, budget: dict, blockers: list, llm_spend_summary: dict,
                     revenue_eur: float, anthropic_authorized: bool = False) -> dict:
    calls = int((llm_spend_summary or {}).get("api_calls", 0)
                or (llm_spend_summary or {}).get("total_api_calls", 0) or 0)
    spent = float((llm_spend_summary or {}).get("total_cost_usd", 0.0) or 0.0)
    paypal = "READY"
    for b in (blockers or []):
        if b.get("area") == "payment" or "PAYEE" in str(b.get("detail", "")).upper():
            paypal = "BLOCKED"
            break
    return {
        "anthropic": {
            "state": "AUTHORIZED" if anthropic_authorized else "DISABLED",
            "api_calls": calls,
            "spent_usd": round(spent, 4),
        },
        "paypal": {"state": paypal},
        "external_spend_usd": round(spent, 2),
        "presale_limit_eur": (budget or {}).get("presale_cap_eur"),
        "presale_limit_usd": (budget or {}).get("presale_cap_usd"),
        "presale_remaining_usd": (budget or {}).get("presale_remaining_usd"),
        "presale_active": (budget or {}).get("presale_active"),
        "revenue_eur": round(float(revenue_eur or 0.0), 2),
        "money_actions": "HUMAN ONLY",
        "can_spend_now": False,
    }


# ---------------------------------------------------------------------------
# profit scale - daily / weekly, real ledger entries only
# ---------------------------------------------------------------------------

# Same implicit rate the pre-sale cap already uses (EUR 3.00 ~= USD 3.20),
# so a $ spend can be shown against EUR revenue without inventing a new rate.
_USD_TO_EUR = 3.00 / 3.20

_DAY_SECONDS = 86400
_WEEK_SECONDS = 7 * _DAY_SECONDS


def _parse_ts(value) -> float | None:
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window_totals(revenue_entries: list, spend_entries: list, *,
                    now_ts: float, window_seconds: int) -> dict:
    cutoff = now_ts - window_seconds
    revenue = 0.0
    for e in revenue_entries or []:
        ts = _parse_ts(e.get("received_at"))
        if ts is not None and ts >= cutoff:
            revenue += float(e.get("amount", 0.0) or 0.0)
    spend_usd = 0.0
    for e in spend_entries or []:
        ts = _parse_ts(e.get("ts"))
        if ts is not None and ts >= cutoff:
            spend_usd += float(e.get("cost_usd", 0.0) or 0.0)
    spend_eur = spend_usd * _USD_TO_EUR
    return {
        "revenue_eur": round(revenue, 2),
        "spend_usd": round(spend_usd, 4),
        "spend_eur": round(spend_eur, 2),
        "profit_eur": round(revenue - spend_eur, 2),
    }


def profit_scale(revenue_entries: list, spend_entries: list, *,
                 now_iso_str: str | None = None) -> dict:
    """Daily (24h) and weekly (7d) profit = booked revenue minus AI spend,
    computed only from real RevenueLedger / LlmSpendLog entries. Deterministic
    given the same entries and `now_iso_str`."""
    from .store import now_iso

    now_ts = _parse_ts(now_iso_str or now_iso()) or 0.0
    daily = _window_totals(revenue_entries, spend_entries,
                           now_ts=now_ts, window_seconds=_DAY_SECONDS)
    weekly = _window_totals(revenue_entries, spend_entries,
                            now_ts=now_ts, window_seconds=_WEEK_SECONDS)
    scale_max = max(1.0, abs(daily["profit_eur"]), abs(weekly["profit_eur"]))
    return {"daily": daily, "weekly": weekly, "scale_max_eur": round(scale_max, 2)}


# ---------------------------------------------------------------------------
# customer / revenue pipeline
# ---------------------------------------------------------------------------

_G, _Y, _R, _O = "green", "amber", "red", "off"


def revenue_pipeline(*, candidate: dict | None, checkout_built: bool,
                     checkout_deployed: bool, paypal_blocked: bool,
                     intake_count: int, plan_count: int, delivered_count: int,
                     revenue_eur: float, leads: int, outreach_ready: int) -> list[dict]:
    """LEAD -> OUTREACH -> CHECKOUT -> PAYPAL -> INTAKE -> PLAN -> PDF ->
    DELIVERY -> REVENUE, each with a real state colour + note."""
    cstatus = (candidate or {}).get("status", "")
    stages: list[dict] = []

    stages.append(("LEAD",
                   _G if leads else _Y,
                   f"{leads} lead(s) on file" if leads else "no leads - run discovery"))
    stages.append(("OUTREACH",
                   _G if outreach_ready else _Y,
                   f"{outreach_ready} draft(s) ready to post"
                   if outreach_ready else "no outreach drafts prepared"))
    if checkout_deployed:
        stages.append(("CHECKOUT", _G, "checkout page is published"))
    elif checkout_built:
        stages.append(("CHECKOUT", _Y, "checkout page built, NOT deployed (human step)"))
    else:
        stages.append(("CHECKOUT", _Y,
                       "checkout not built - run `build-checkout` (human)"))
    stages.append(("PAYPAL",
                   _R if paypal_blocked else _G,
                   "PAYEE_ACCOUNT_RESTRICTED - cannot capture payments"
                   if paypal_blocked else "ready to capture"))
    stages.append(("INTAKE",
                   _G if intake_count else _Y,
                   f"{intake_count} buyer intake row(s)"
                   if intake_count else "waiting for a paid order"))
    stages.append(("PLAN",
                   _G if plan_count else _Y,
                   f"{plan_count} launch plan(s) drafted"
                   if plan_count else "waiting for intake"))
    stages.append(("PDF",
                   _G if delivered_count else _Y,
                   "rendered" if delivered_count else "waiting for plan"))
    stages.append(("DELIVERY",
                   _G if delivered_count else _Y,
                   f"{delivered_count} delivered" if delivered_count
                   else "waiting for PDF"))
    stages.append(("REVENUE",
                   _G if revenue_eur > 0 else _Y,
                   f"EUR {revenue_eur:.2f} booked" if revenue_eur > 0
                   else "no revenue yet"))

    if cstatus:
        stages[0] = ("LEAD", stages[0][1],
                     f"{stages[0][2]} - candidate is '{cstatus}'")
    return [{"stage": s, "state": st, "note": n} for s, st, n in stages]


# ---------------------------------------------------------------------------
# acquisition view
# ---------------------------------------------------------------------------

def _lead_age_days(lead: dict) -> int | None:
    v = lead.get("age_days")
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _lead_score(lead: dict) -> float:
    for k in ("final_score", "relevance_score", "fit_score", "score"):
        v = lead.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return 0.0


def acquisition_view(*, leads: list, briefs: list, last_discovery: dict | None,
                     fresh_days: int = 7, high_score: float = 60.0) -> dict:
    leads = [l for l in (leads or []) if isinstance(l, dict)]
    briefs = [b for b in (briefs or []) if isinstance(b, dict)]
    drafted_ids = {b.get("lead_id") for b in briefs}
    awaiting = [b for b in briefs if b.get("status") in ("draft", "approved")]

    rows: list[dict] = []
    fresh = stale = high = 0
    for l in sorted(leads, key=_lead_score, reverse=True):
        age = _lead_age_days(l)
        score = _lead_score(l)
        is_fresh = age is not None and age <= fresh_days
        is_high = score >= high_score
        fresh += is_fresh
        stale += (age is not None and age > fresh_days)
        high += is_high
        lid = l.get("lead_id", "")
        has_draft = lid in drafted_ids
        if is_fresh and is_high and not has_draft:
            rec = "Prepare Outreach - fresh, high-quality"
        elif has_draft:
            rec = "Draft ready - review and post it yourself"
        elif age is not None and age > 30:
            rec = "Skip - too old to convert"
        elif not is_high:
            rec = "Low relevance - probably skip"
        else:
            rec = "Prepare Outreach"
        rows.append({
            "lead_id": lid,
            "score": round(score, 1),
            "signal": (l.get("title") or l.get("url") or "")[:90],
            "age_days": age,
            "age_bucket": l.get("age_bucket", "unknown"),
            "source": l.get("source") or l.get("platform", ""),
            "status": ("drafted" if has_draft else "new"),
            "recommended_action": rec,
            "has_draft": has_draft,
        })

    return {
        "total": len(leads),
        "fresh": fresh,
        "stale": stale,
        "high_quality": high,
        "awaiting_outreach": len(awaiting),
        "outreach_drafts": len(briefs),
        "last_discovery": last_discovery,
        "leads": rows,
    }
