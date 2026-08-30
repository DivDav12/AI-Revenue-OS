"""Autopilot - one orchestrator that chains the EXISTING pieces.

It does NOT replace any agent or store. One `run_cycle()` call:

  1. free discovery  (prospect_scout -> opportunity_scorer via
                       workflow.discover_acquisition_opportunities)
  2. outreach briefs  (outreach.outreach_brief) for high/medium-quality leads,
     then a durable, de-duped ACQUISITION REVIEW QUEUE of every prospect
     still waiting on a human (`acquisition_queue()` / `acquisition-queue`)
  3. payment check    (paypal.sync_transactions -> RevenueLedger)   [read-only booking]
  4. intake / plan    (existing intake + LaunchPlanAgent, POST-SALE budget only)
  5. delivery queue   (plan approved but not rendered)

Then it compiles a human action queue and stops at every point that
needs a person: posting public replies, approving a plan, sending a PDF.

State is persisted to data/autopilot.json and survives a restart. The
autopilot never posts, messages, emails, or spends past the EUR 3.00
pre-sale cap (budget.guard, enforced in llm_workers.budget_gate).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .store import now_iso

logger = logging.getLogger(__name__)

_STATES = ("stopped", "running", "paused")
_QUALITY_FOR_OUTREACH = ("high", "medium")


class AutopilotState:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict = {
            "status": "stopped",
            "checkout_url": None,
            "cycles": 0,
            "started_at": None,
            "last_cycle_at": None,
            "last_report": None,
            "pause_reason": None,
        }

    @classmethod
    def load(cls, path: str | Path) -> "AutopilotState":
        s = cls(path)
        if s.path.exists():
            try:
                raw = json.loads(s.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    s.data.update(raw)
            except json.JSONDecodeError:
                logger.warning("corrupt autopilot state - starting fresh")
        return s

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.data, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def _state(data_dir) -> AutopilotState:
    return AutopilotState.load(Path(data_dir) / "autopilot.json")


# --- acquisition review queue -------------------------------------------

_ACQ_DONE_BRIEF = ("posted", "skipped")


def acquisition_queue(data_dir) -> list[dict]:
    """Every high/medium-quality prospect that still needs a human, with the
    facts a person needs to act on it: their own words, the community's
    promo policy, and the drafted brief's status.

    Recomputed from the two stores each call (no separate state to drift):
      - a lead the human REJECTED, or whose brief is `posted`/`skipped`,
        is finished and never re-surfaces;
      - `stage` is `prepared` once a draft brief exists, else `surfaced`.
    Ranked by the lead's final_score (freshest genuine asks first).
    """
    from .acquisition import AcquisitionStore
    from .outreach import OutreachStore

    data_dir = Path(data_dir)
    leads = AcquisitionStore.load(data_dir / "acquisition.json").ranked()
    briefs = OutreachStore.load(data_dir / "outreach.json")

    queue: list[dict] = []
    for lead in leads:
        if lead.get("prospect_quality") not in _QUALITY_FOR_OUTREACH:
            continue
        lid = lead.get("lead_id")
        brief = briefs.get(lid)
        brief_status = (brief or {}).get("status")
        if (lead.get("human_review_status") == "rejected"
                or brief_status in _ACQ_DONE_BRIEF):
            continue
        queue.append({
            "lead_id": lid,
            "url": lead.get("url", ""),
            "platform": lead.get("platform", ""),
            "prospect_quality": lead.get("prospect_quality"),
            "prospect_type": lead.get("prospect_type"),
            "age_days": lead.get("age_days"),
            "age_bucket": lead.get("age_bucket", "unknown"),
            "final_score": lead.get("final_score", 0),
            "their_words": str(lead.get("problem_summary")
                               or lead.get("title") or "")[:240],
            "promo_allowed": lead.get("promo_allowed", "unknown"),
            "promo_note": lead.get("promo_note", ""),
            "human_review_status": lead.get("human_review_status", "new"),
            "brief_status": brief_status or "none",
            "stage": "prepared" if brief else "surfaced",
            "next_action": (
                "review the drafted brief, check this community's rules, and "
                "post your own helpful reply" if brief
                else "a brief will be drafted on the next cycle"),
        })
    return queue


def pause(data_dir, reason: str = "manual pause") -> dict:
    st = _state(data_dir)
    st.data["status"] = "paused"
    st.data["pause_reason"] = reason
    st.save()
    return st.data


def resume(data_dir) -> dict:
    st = _state(data_dir)
    st.data["status"] = "running"
    st.data["pause_reason"] = None
    st.save()
    return st.data


def stop(data_dir) -> dict:
    st = _state(data_dir)
    st.data["status"] = "stopped"
    st.save()
    return st.data


def status(data_dir) -> dict:
    from . import budget
    from .acquisition import AcquisitionStore
    from .outreach import OutreachStore
    from .revenue import RevenueLedger

    st = _state(data_dir).data
    leads = AcquisitionStore.load(Path(data_dir) / "acquisition.json").all()
    briefs = OutreachStore.load(Path(data_dir) / "outreach.json").all()
    rev = RevenueLedger.load(Path(data_dir) / "revenue.json")
    hi = [l for l in leads if l.get("prospect_quality") in _QUALITY_FOR_OUTREACH]
    return {
        "autopilot": st["status"],
        "acquisition_queue_len": len(acquisition_queue(data_dir)),
        "pause_reason": st.get("pause_reason"),
        "cycles": st.get("cycles", 0),
        "last_cycle_at": st.get("last_cycle_at"),
        "capital": budget.status(data_dir),
        "leads_total": len(leads),
        "leads_high_quality": len(hi),
        "leads_awaiting_review": sum(
            1 for l in leads if l.get("human_review_status") == "new"),
        "leads_reviewed": sum(
            1 for l in leads if l.get("human_review_status") == "reviewed"),
        "outreach_briefs": len(briefs),
        "outreach_awaiting_post": sum(
            1 for b in briefs if b.get("status") in ("draft", "approved")),
        "outreach_posted": sum(1 for b in briefs if b.get("status") == "posted"),
        "customers_paid": len(rev.entries()),
        "revenue_eur": round(rev.total(), 2),
    }


def _paypal_check(data_dir) -> dict:
    """Read-only: book any new LIVE PayPal capture through the existing
    rules. Missing credentials is a note, not a crash."""
    import os as _os

    from .paypal import PayPalConfig, sync_transactions
    from .revenue import RevenueLedger
    from .store import CandidateStore

    if not (_os.environ.get("PAYPAL_CLIENT_ID")
            and _os.environ.get("PAYPAL_CLIENT_SECRET")):
        return {"ok": False, "note": "PAYPAL_CLIENT_ID / _SECRET not in the "
                "environment - payment check skipped (source .env first)"}
    try:
        PayPalConfig.from_env()
        store = CandidateStore.load(Path(data_dir) / "candidates.json")
        ledger = RevenueLedger.load(Path(data_dir) / "revenue.json")
        r = sync_transactions(store, ledger, days=7, dry_run=False)
        return {"ok": True, "booked": r["booked"], "skipped": r["skipped"],
                "total_booked": r["total_booked"]}
    except Exception as exc:  # network / auth / billing - pause-worthy, not fatal
        return {"ok": False, "note": f"PayPal check failed: {exc}"}


def run_cycle(data_dir, *, allow_web: bool = False, max_age_days: int = 14,
              limit: int = 15, politeness_delay: float = 1.0,
              checkout_url: str | None = None) -> dict:
    """Advance the funnel as far as it can without a human, then report
    what a human must do next. Idempotent."""
    from . import budget
    from .acquisition import SEARCH_QUERIES, AcquisitionStore
    from .acquisition_sources import FREE_SOURCES, build_acquisition_source
    from .outreach import DEFAULT_CHECKOUT_URL, OutreachStore, outreach_brief
    from .workflow import discover_acquisition_opportunities

    data_dir = Path(data_dir)
    st = _state(data_dir)
    if checkout_url:
        st.data["checkout_url"] = checkout_url
    ck_url = st.data.get("checkout_url") or DEFAULT_CHECKOUT_URL

    if st.data["status"] == "paused":
        return {"skipped": "autopilot is paused", "pause_reason":
                st.data.get("pause_reason"), "status": status(data_dir)}

    st.data["status"] = "running"
    st.data["started_at"] = st.data.get("started_at") or now_iso()
    report: dict = {"actions": [], "notes": [], "spend": budget.status(data_dir),
                    "sale": False}

    # --- 1. discovery (free by default; web only if explicitly allowed) ---
    names = list(FREE_SOURCES) + (["web"] if allow_web else [])
    web_source = web_cache = None
    try:
        if allow_web:
            from .llm_cache import LlmCache
            from .llm_normalize import build_client
            from .acquisition_web import WebSearchSource
            est = round(0.05 * len(SEARCH_QUERIES), 4)
            budget.guard(data_dir, est)            # pre-sale hard cap
            ceiling = min(est * 3, budget.presale_remaining_usd(data_dir)
                          if budget.presale_active(data_dir) else 1.0)
            web_cache = LlmCache.load(data_dir / "llm_acquisition_web_cache.json")
            web_source = WebSearchSource(client=build_client(), max_cost_usd=ceiling,
                                        cache=web_cache)
        source = build_acquisition_source(names, web_source=web_source)
        store = AcquisitionStore.load(data_dir / "acquisition.json")
        r = discover_acquisition_opportunities(
            store, source, queries=SEARCH_QUERIES, limit=limit,
            min_score=0, max_age_days=max_age_days,
            politeness_delay=politeness_delay)
        if web_cache is not None:
            web_cache.save()
        if web_source is not None:
            from .llm_workers import record_llm_spend
            record_llm_spend(data_dir, "acquisition", web_source)
        report["discovery"] = {
            "scored": len(r["leads"]), "new": r["new"], "updated": r["updated"],
            "sources_status": r["sources_status"],
            "web_cost_usd": round(web_source.meter.cost_usd, 4) if web_source else 0.0,
        }
    except budget.BudgetBlocked as exc:
        report["notes"].append(str(exc))
        report["discovery"] = {"skipped": "budget"}
    except Exception as exc:
        logger.warning("discovery failed: %s", exc)
        report["notes"].append(f"discovery error: {exc}")
        report["discovery"] = {"error": str(exc)}

    # --- 2. outreach briefs for high/medium-quality leads ---------------
    store = AcquisitionStore.load(data_dir / "acquisition.json")
    briefs = OutreachStore.load(data_dir / "outreach.json")
    prepared = 0
    for lead in store.ranked():
        if lead.get("prospect_quality") not in _QUALITY_FOR_OUTREACH:
            continue
        if lead.get("human_review_status") == "rejected":
            continue
        if briefs.has(lead.get("lead_id")):
            continue
        briefs.put(outreach_brief(lead, checkout_url=ck_url))
        prepared += 1
    if prepared:
        briefs.save()

    # --- 2b. the acquisition review queue (durable, de-duped) ----------
    queue = acquisition_queue(data_dir)
    report["acquisition_queue"] = queue
    report["outreach"] = {
        "prepared": prepared,
        "awaiting_post": sum(1 for b in briefs.all()
                             if b.get("status") in ("draft", "approved")),
        "queue_len": len(queue),
    }
    if queue:
        report["actions"].append(
            f"HUMAN: {len(queue)} outreach prospect(s) awaiting you - review "
            "each drafted brief, check the community's rules, and post your "
            "own reply (`revenue_os acquisition-queue`). The system never posts.")

    # --- 3. payment check (LIVE, read-only booking) --------------------
    pp = _paypal_check(data_dir)
    report["payment"] = pp
    if not pp["ok"]:
        report["notes"].append(pp["note"])
    elif pp.get("booked"):
        report["sale"] = True
        report["actions"].append(
            "NEW PAYMENT(S) booked. Pre-sale mode is now OFF - see "
            "`autopilot status`.")

    # --- 4. intake / plan / delivery status (human gates) -------------
    _funnel_status(data_dir, report)

    # --- finalise ---
    st.data["cycles"] = int(st.data.get("cycles", 0)) + 1
    st.data["last_cycle_at"] = now_iso()
    st.data["last_report"] = report
    if not report["actions"] and report["outreach"]["awaiting_post"] == 0:
        report["idle"] = "AUTOPILOT IDLE - waiting for the next discovery cycle"
    st.save()
    report["status"] = status(data_dir)
    return report


def _funnel_status(data_dir, report: dict) -> None:
    intake_path = Path(data_dir) / "intake.json"
    if not intake_path.exists():
        report["intake"] = {"submissions": 0}
        return
    from .intake import IntakeStore

    it = IntakeStore.load(intake_path)
    subs = it.all()
    report["intake"] = {"submissions": len(subs)}
    for e in subs:
        oid = e.get("order_id")
        st = e.get("status")
        plan = e.get("plan") or {}
        if st == "new":
            report["actions"].append(
                f"HUMAN: review intake {oid} -> `revenue_os intake-review {oid}`")
        elif st == "reviewed" and not plan:
            report["actions"].append(
                f"HUMAN/AI: draft the Customer Launch Plan for {oid} "
                f"(`revenue_os draft-launch-plan {oid}` - POST-SALE budget)")
        elif plan.get("status") == "draft":
            report["actions"].append(
                f"HUMAN: QC + approve the plan for {oid} "
                f"-> `revenue_os plan-approve {oid}`")
        elif plan.get("status") == "approved":
            report["actions"].append(
                f"HUMAN: render + deliver the plan for {oid} "
                f"-> `revenue_os plan-render {oid}`, then email the PDF")
